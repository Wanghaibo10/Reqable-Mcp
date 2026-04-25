"""Rule engine for Phase 2 traffic-modification tools.

A *rule* is a declarative match-and-apply spec the user installs via
MCP tools (``tag_pattern``, ``inject_header``, ``mock_response``, …).
The daemon stores rules in memory and on disk; the per-request
addons.py asks the daemon over the IPC socket which rules apply, then
mutates the request/response object accordingly.

Rules carry a TTL so a forgotten rule can't silently rewrite traffic
forever — a Phase 2 safety requirement called out in spec.md.

Persistence: ``rules.json`` is rewritten atomically (tempfile +
``os.replace``) on every mutation. Rule IDs are uuid4 hex.

This module does no I/O of its own beyond the JSON load/save; tests
cover it without spinning a socket.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

log = logging.getLogger(__name__)

RuleKind = Literal[
    "tag",
    "comment",
    "inject_header",
    "replace_body",
    "mock",
    "block",
    "relay_extract",
    "relay_inject",
    "patch_field",
    "regex_replace",
]
RuleSide = Literal["request", "response"]

VALID_KINDS: frozenset[str] = frozenset(
    (
        "tag", "comment", "inject_header", "replace_body", "mock", "block",
        "relay_extract", "relay_inject",
        "patch_field", "regex_replace",
    )
)
VALID_SIDES: frozenset[str] = frozenset(("request", "response"))

# Hard upper bound for a rule's TTL. Defends against "ttl_seconds=99999"
# typos rewriting traffic for hours unattended.
MAX_TTL_SECONDS: int = 3600
DEFAULT_TTL_SECONDS: int = 300

# Cap for any body payload carried inside a rule (replace_body, mock).
# IPC frame limit is 256 KB; one request may match several rules, so
# we keep individual bodies well under that. 64 KB is plenty for the
# JSON / HTML payloads people typically mock.
BODY_MAX_BYTES: int = 64 * 1024


@dataclass
class Rule:
    """One installed rule.

    ``host`` matches ``Context.host`` exactly (lower-cased). ``None``
    means "any host". ``path_pattern`` is a Python regex matched with
    ``re.search`` against the request path (``HttpRequest.path``).
    ``method`` is upper-cased and exact-match.

    ``payload`` carries the kind-specific fields verbatim; we don't
    re-validate addons-side fields on the daemon (the addons template
    accepts known shapes only). Schema:

    * ``tag``           : {"color": "red" | ... }
    * ``comment``       : {"text": "..."}
    * ``inject_header`` : {"name": "...", "value": "..."}
    * ``replace_body``  : {"body": "..."}     (text only for now)
    * ``mock``          : {"status": int, "body": "...", "headers": {...}}
    * ``block``         : {}                   (addons raises to abort)
    """

    id: str
    kind: RuleKind
    side: RuleSide
    host: str | None
    path_pattern: str | None
    method: str | None
    payload: dict[str, Any]
    created_ts: float
    expires_ts: float | None
    hits: int = 0
    # dry_run=True turns off the mutation half of every rule kind: the
    # addons hook still records that the rule matched (it bumps hits
    # and reports a structured "would have done X" event the operator
    # can fetch via ``dry_run_log``), but the in-flight message is left
    # untouched. Critical for vetting a regex / path_pattern before
    # letting it really rewrite traffic.
    dry_run: bool = False
    _path_re: re.Pattern[str] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.path_pattern:
            try:
                self._path_re = re.compile(self.path_pattern)
            except re.error as e:
                raise ValueError(f"invalid path_pattern regex: {e}") from e

    def matches(
        self, *, side: str, host: str | None, path: str | None, method: str | None
    ) -> bool:
        if side != self.side:
            return False
        if self.host is not None and (host or "").lower() != self.host:
            return False
        if self.method is not None and (method or "").upper() != self.method:
            return False
        return not (self._path_re is not None and not self._path_re.search(path or ""))

    def is_expired(self, *, now: float | None = None) -> bool:
        if self.expires_ts is None:
            return False
        return (now if now is not None else time.time()) >= self.expires_ts

    def to_addon_payload(self) -> dict[str, Any]:
        """Shape sent down to addons.py — id + kind + payload, no metadata."""
        out: dict[str, Any] = {"id": self.id, "kind": self.kind}
        if self.dry_run:
            out["dry_run"] = True
        out.update(self.payload)
        return out

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("_path_re", None)
        return d

    # Type spec used by ``from_dict`` to reject corrupt persisted rows
    # before they reach the dataclass constructor (which would silently
    # accept e.g. ttl="not a number" until used). ClassVar so dataclass
    # doesn't try to treat it as an instance field.
    _FIELD_TYPES: ClassVar[dict[str, type | tuple[type, ...]]] = {
        "id": str,
        "kind": str,
        "side": str,
        "host": (str, type(None)),
        "path_pattern": (str, type(None)),
        "method": (str, type(None)),
        "payload": dict,
        "created_ts": (int, float),
        "expires_ts": (int, float, type(None)),
        "hits": int,
        "dry_run": bool,
    }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Rule:
        if not isinstance(d, dict):
            raise ValueError(f"rule entry must be a dict, got {type(d).__name__}")
        # Validate known fields; ignore unknown ones (forward-compat with
        # rules.json written by future versions).
        for fname, ftypes in cls._FIELD_TYPES.items():
            if fname in d and not isinstance(d[fname], ftypes):
                raise ValueError(
                    f"field {fname!r} has wrong type: "
                    f"{type(d[fname]).__name__} (expected {ftypes})"
                )
        kwargs = {k: v for k, v in d.items() if k in cls._FIELD_TYPES}
        return cls(**kwargs)


def _validated_ttl(ttl_seconds: int | None) -> float | None:
    if ttl_seconds is None:
        return None
    if not isinstance(ttl_seconds, int):
        raise ValueError(f"ttl_seconds must be int, got {type(ttl_seconds).__name__}")
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be > 0")
    if ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds {ttl_seconds} exceeds MAX_TTL_SECONDS={MAX_TTL_SECONDS}; "
            "use a shorter TTL or call clear_rules() to disable manually"
        )
    return time.time() + ttl_seconds


class RuleEngine:
    """Thread-safe in-memory rule store with JSON persistence.

    Designed for Reqable's fork-per-request addons calling
    :meth:`match_for` very frequently (a few hundred times/min on a
    busy host). The lock window is short — rules dict copy + filter —
    so concurrent IPC handlers don't pile up.
    """

    def __init__(self, path: Path, *, autoload: bool = True):
        self.path = Path(path)
        self._rules: dict[str, Rule] = {}
        self._lock = threading.Lock()
        if autoload:
            self.load()

    # ------------------------------------------------------------------ load/save

    def load(self) -> None:
        """Restore rules from disk, dropping any that are already expired.

        Idempotent — re-call to merge new on-disk rules into memory.
        Called automatically from ``__init__`` unless ``autoload=False``.
        """
        if not self.path.exists():
            return
        try:
            with self.path.open() as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning("rules.json unreadable (%s); starting empty", e)
            return
        rules = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(rules, list):
            log.warning("rules.json has unexpected shape; ignoring")
            return
        now = time.time()
        with self._lock:
            for rd in rules:
                try:
                    r = Rule.from_dict(rd)
                except (TypeError, ValueError) as e:
                    log.warning("dropping invalid persisted rule: %s", e)
                    continue
                if r.is_expired(now=now):
                    continue
                self._rules[r.id] = r

    def _save_locked(self) -> None:
        """Caller holds ``self._lock``."""
        payload = {"rules": [r.to_dict() for r in self._rules.values()]}
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, prefix=".rules.", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------------ mutators

    def add(
        self,
        *,
        kind: RuleKind,
        side: RuleSide,
        payload: dict[str, Any],
        host: str | None = None,
        path_pattern: str | None = None,
        method: str | None = None,
        ttl_seconds: int | None = DEFAULT_TTL_SECONDS,
        dry_run: bool = False,
    ) -> Rule:
        if kind not in VALID_KINDS:
            raise ValueError(f"invalid kind {kind!r}; must be one of {sorted(VALID_KINDS)}")
        if side not in VALID_SIDES:
            raise ValueError(f"invalid side {side!r}; must be 'request' or 'response'")
        # Block + mock are response-side intent but block lives on request.
        if kind == "mock" and side != "response":
            raise ValueError("kind='mock' must use side='response'")
        if kind == "block" and side != "request":
            raise ValueError("kind='block' must use side='request'")
        if kind == "relay_extract" and side != "response":
            raise ValueError("kind='relay_extract' must use side='response'")
        if kind == "relay_inject" and side != "request":
            raise ValueError("kind='relay_inject' must use side='request'")
        expires = _validated_ttl(ttl_seconds)
        rule = Rule(
            id=uuid.uuid4().hex,
            kind=kind,
            side=side,
            host=host.lower() if host else None,
            path_pattern=path_pattern,
            method=method.upper() if method else None,
            payload=dict(payload),
            created_ts=time.time(),
            expires_ts=expires,
            dry_run=bool(dry_run),
        )
        with self._lock:
            self._rules[rule.id] = rule
            self._save_locked()
        return rule

    def remove(self, rule_id: str) -> bool:
        with self._lock:
            existed = self._rules.pop(rule_id, None) is not None
            if existed:
                self._save_locked()
            return existed

    def clear(self) -> int:
        with self._lock:
            n = len(self._rules)
            self._rules.clear()
            self._save_locked()
            return n

    def reap_expired(self) -> int:
        """Drop expired rules; returns how many were removed."""
        now = time.time()
        with self._lock:
            stale = [r.id for r in self._rules.values() if r.is_expired(now=now)]
            if not stale:
                return 0
            for rid in stale:
                del self._rules[rid]
            self._save_locked()
        return len(stale)

    def record_hit(self, rule_id: str) -> bool:
        """Bump hit count; called from IPC ``report_hit`` handler."""
        with self._lock:
            r = self._rules.get(rule_id)
            if r is None:
                return False
            r.hits += 1
            # Don't fsync on every hit — record_hit is best-effort and
            # the persisted hit count is informational. Save lazily.
            return True

    # ------------------------------------------------------------------ readers

    def list_all(self, kind: RuleKind | None = None) -> list[Rule]:
        with self._lock:
            now = time.time()
            return [
                r
                for r in self._rules.values()
                if not r.is_expired(now=now) and (kind is None or r.kind == kind)
            ]

    def match_for(
        self,
        *,
        side: str,
        host: str | None,
        path: str | None,
        method: str | None,
    ) -> list[Rule]:
        """Return rules to apply for this in-flight request/response.

        Filters out expired rules. Caller should call :meth:`record_hit`
        on each rule it actually applied.
        """
        now = time.time()
        with self._lock:
            return [
                r
                for r in self._rules.values()
                if not r.is_expired(now=now)
                and r.matches(side=side, host=host, path=path, method=method)
            ]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            active = [r for r in self._rules.values() if not r.is_expired(now=now)]
            total_hits = sum(r.hits for r in active)
            by_kind: dict[str, int] = {}
            for r in active:
                by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
            return {
                "active": len(active),
                "expired_pending_reap": len(self._rules) - len(active),
                "total_hits": total_hits,
                "by_kind": by_kind,
            }


__all__ = [
    "BODY_MAX_BYTES",
    "DEFAULT_TTL_SECONDS",
    "MAX_TTL_SECONDS",
    "Rule",
    "RuleEngine",
    "RuleKind",
    "RuleSide",
    "VALID_KINDS",
    "VALID_SIDES",
]
