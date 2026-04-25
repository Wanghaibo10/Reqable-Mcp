"""In-memory relay store for ``auto_token_relay``.

We deliberately do **not** depend on Reqable SDK's ``context.env``
field — its cross-capture persistence is undocumented and addons
already proves it can disappear between forks. Instead, the addons
hook stores extracted tokens in this daemon-side dict over IPC, and
fetches them back on a later request.

Properties:

* TTL — every value expires after a caller-specified deadline so
  forgotten relays don't leak tokens forever.
* Thread-safe — addons may extract from a response and inject into
  the next request concurrently across worker threads.
* Volatile — values live in memory only. A daemon restart wipes
  everything (this is a feature: relayed tokens never outlive the
  process that captured them).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# Hard upper bound mirrors the rule TTL cap. Reusing it keeps the
# user's mental model simple: "all my Phase-2 stuff has the same
# upper-bound TTL".
MAX_RELAY_TTL_SECONDS: int = 3600
DEFAULT_RELAY_TTL_SECONDS: int = 300

# Cap for a single relay value. A token is usually < 4 KB; bigger is
# almost always a bug or an attempt to misuse this for body relay.
MAX_RELAY_VALUE_BYTES: int = 8 * 1024


@dataclass
class RelayEntry:
    value: str
    expires_ts: float


class RelayStore:
    """Thread-safe TTL'd ``name → value`` map for relay tokens."""

    def __init__(self) -> None:
        self._values: dict[str, RelayEntry] = {}
        self._lock = threading.Lock()

    def set(self, name: str, value: str, *, ttl_seconds: int) -> None:
        if not name:
            raise ValueError("relay name must be non-empty")
        if not isinstance(value, str):
            raise TypeError(f"relay value must be str, got {type(value).__name__}")
        if len(value.encode("utf-8")) > MAX_RELAY_VALUE_BYTES:
            raise ValueError(
                f"relay value exceeds MAX_RELAY_VALUE_BYTES="
                f"{MAX_RELAY_VALUE_BYTES}"
            )
        if ttl_seconds <= 0 or ttl_seconds > MAX_RELAY_TTL_SECONDS:
            raise ValueError(
                f"ttl_seconds must be in (0, {MAX_RELAY_TTL_SECONDS}]"
            )
        with self._lock:
            self._values[name] = RelayEntry(value, time.time() + ttl_seconds)

    def get(self, name: str) -> str | None:
        """Returns the stored value, or None if absent / expired.

        Lazy-expiration: an expired entry is removed when seen.
        """
        with self._lock:
            entry = self._values.get(name)
            if entry is None:
                return None
            if time.time() >= entry.expires_ts:
                del self._values[name]
                return None
            return entry.value

    def remove(self, name: str) -> bool:
        with self._lock:
            return self._values.pop(name, None) is not None

    def clear(self) -> int:
        with self._lock:
            n = len(self._values)
            self._values.clear()
            return n

    def reap_expired(self) -> int:
        """Drop entries whose ``expires_ts`` is in the past. Returns count."""
        now = time.time()
        with self._lock:
            stale = [k for k, v in self._values.items() if now >= v.expires_ts]
            for k in stale:
                del self._values[k]
            return len(stale)

    def stats(self) -> dict[str, int]:
        with self._lock:
            now = time.time()
            active = sum(1 for v in self._values.values() if now < v.expires_ts)
            return {"total": len(self._values), "active": active}

    def list_names(self) -> list[str]:
        """For debugging / status. Returns NAMES only — never values
        (we'd rather a status dump leak nothing sensitive)."""
        with self._lock:
            now = time.time()
            return [
                k for k, v in self._values.items() if now < v.expires_ts
            ]


__all__ = [
    "DEFAULT_RELAY_TTL_SECONDS",
    "MAX_RELAY_TTL_SECONDS",
    "MAX_RELAY_VALUE_BYTES",
    "RelayEntry",
    "RelayStore",
]
