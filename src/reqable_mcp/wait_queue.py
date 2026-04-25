"""In-process wait queue for ``wait_for(...)`` MCP tool.

Lets a Claude Code conversation block until a matching capture appears
("user, click the login button now — I'll wait for ``auth.api/login``").

Implementation
--------------
Simple thread-safe registry keyed by a generated waiter id. The
:class:`LmdbSource` poller calls :meth:`WaitQueue.notify` for every
new capture; each notify walks active waiters and ``set()`` s the
event of any whose filter matches. The MCP tool side calls
:meth:`wait` with a timeout.

This is intentionally process-local — when Claude Code disconnects,
the MCP server shuts down and any pending waits are cancelled. We
don't try to persist them.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class FilterSpec:
    """Conditions a capture must satisfy to wake a waiter.

    All conditions are AND-combined. ``None`` means "any".

    ``path_pattern`` is a Python regex. It's matched with ``re.search``
    against the full URL (so callers can match either path or full URL
    naturally).
    """

    host: str | None = None
    method: str | None = None
    path_pattern: str | None = None
    app: str | None = None
    status: int | None = None

    _path_re: re.Pattern[str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.path_pattern is not None:
            try:
                self._path_re = re.compile(self.path_pattern)
            except re.error as e:
                raise ValueError(f"invalid path_pattern regex: {e}") from e
        if self.method is not None:
            # Normalize so "post" and "POST" both work
            self.method = self.method.upper()

    def matches(self, capture: dict[str, Any]) -> bool:
        if self.host is not None and capture.get("host") != self.host:
            return False
        if self.method is not None and capture.get("method") != self.method:
            return False
        if self.app is not None and capture.get("app_name") != self.app:
            return False
        if self.status is not None and capture.get("status") != self.status:
            return False
        if self._path_re is not None:
            url = capture.get("url") or capture.get("path") or ""
            if not self._path_re.search(url):
                return False
        return True


@dataclass
class _Waiter:
    """One pending ``wait_for`` call. Internal."""

    id: str
    spec: FilterSpec
    event: threading.Event
    matched: dict[str, Any] | None = None
    created_ts: float = field(default_factory=time.time)


class WaitQueue:
    """Thread-safe waiter registry.

    Multiple concurrent waiters are supported and notified independently;
    the same capture event can wake any number of matching waiters.
    """

    def __init__(self) -> None:
        self._waiters: dict[str, _Waiter] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public

    def add(self, spec: FilterSpec) -> str:
        """Register a new waiter. Returns its id."""
        wid = uuid.uuid4().hex
        w = _Waiter(id=wid, spec=spec, event=threading.Event())
        with self._lock:
            self._waiters[wid] = w
        return wid

    def cancel(self, waiter_id: str) -> None:
        """Remove a waiter without delivering a result.

        Idempotent — silently no-ops if already gone.
        """
        with self._lock:
            self._waiters.pop(waiter_id, None)

    def notify(self, capture: dict[str, Any]) -> int:
        """Deliver a capture to any matching waiter(s).

        Returns the number woken. Waiters that match are stamped with
        ``matched`` and signaled. ``wait()`` does the actual cleanup so
        a slow caller can still observe its match.

        Currently called from a single producer thread (the LMDB
        poller). The matched-stamping happens inside ``self._lock`` so
        that if a future change introduces a second producer, the
        first-writer-wins guarantee still holds.
        """
        woken = 0
        with self._lock:
            for w in list(self._waiters.values()):
                if w.matched is not None:
                    continue
                try:
                    if not w.spec.matches(capture):
                        continue
                except Exception:
                    log.exception("waiter %s matcher raised", w.id)
                    self._waiters.pop(w.id, None)
                    continue
                w.matched = capture
                w.event.set()
                woken += 1
        return woken

    def wait(
        self, waiter_id: str, *, timeout: float
    ) -> dict[str, Any] | None:
        """Block up to ``timeout`` seconds. Return the capture or None.

        Whether matched or timed out, the waiter is unregistered before
        we return.
        """
        with self._lock:
            w = self._waiters.get(waiter_id)
        if w is None:
            return None
        w.event.wait(timeout=timeout)
        with self._lock:
            self._waiters.pop(waiter_id, None)
        return w.matched

    # ------------------------------------------------------------------ ops/diag

    def active_count(self) -> int:
        with self._lock:
            return len(self._waiters)

    def reap_expired(self, *, max_age_s: float) -> int:
        """Drop waiters older than ``max_age_s``. Defensive cleanup if a
        caller dropped its waiter id without cancelling.
        """
        cutoff = time.time() - max_age_s
        removed = 0
        with self._lock:
            stale = [
                wid for wid, w in self._waiters.items() if w.created_ts < cutoff
            ]
            for wid in stale:
                w = self._waiters.pop(wid, None)
                if w is not None:
                    w.event.set()  # release any thread still blocked
                    removed += 1
        return removed


__all__ = ["FilterSpec", "WaitQueue"]
