"""Ring-buffered log of dry-run rule matches.

When a rule has ``dry_run=True``, the addons hook records that the
rule matched but does NOT mutate the in-flight message. The match is
reported back here over IPC; an operator fetches the log via the
``dry_run_log`` MCP tool to see "what would have happened" before
flipping the rule to live.

Storage shape:

    {rule_id -> deque(maxlen=PER_RULE_LIMIT)}

Each entry: ``{ts, uid, host, path, method, side}``. We deliberately
do NOT store the rule's *intended* mutation payload here — that's
already in ``rule.payload`` and ``list_rules()`` returns it. Storing
it twice would just inflate memory and risk leaking sensitive
override values into a status snapshot.

Volatile by design: a daemon restart wipes the log. dry-run feedback
is meant for the immediate "tweak the regex" loop, not long-term
audit.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass

# Per-rule cap. 256 ≈ "watch a few minutes of high-traffic browsing
# without filling memory." If a dry-run rule matches more than this
# during a probe, the oldest entries roll off.
PER_RULE_LIMIT: int = 256

# Total cap across all rules. Defends against many dry-run rules
# multiplying the per-rule limit.
TOTAL_RULES_LIMIT: int = 64


@dataclass
class DryRunEntry:
    ts: float
    uid: str
    host: str
    path: str
    method: str
    side: str


class DryRunLog:
    """Thread-safe ring-buffered log of dry-run matches per rule."""

    def __init__(self) -> None:
        self._buckets: dict[str, deque[DryRunEntry]] = {}
        self._lock = threading.Lock()

    def record(
        self,
        *,
        rule_id: str,
        uid: str,
        host: str,
        path: str,
        method: str,
        side: str,
    ) -> None:
        if not rule_id:
            return
        entry = DryRunEntry(
            ts=time.time(), uid=uid, host=host, path=path,
            method=method, side=side,
        )
        with self._lock:
            bucket = self._buckets.get(rule_id)
            if bucket is None:
                # Cap distinct rule_ids so a churned-rule daemon
                # doesn't grow forever.
                if len(self._buckets) >= TOTAL_RULES_LIMIT:
                    return
                bucket = deque(maxlen=PER_RULE_LIMIT)
                self._buckets[rule_id] = bucket
            bucket.append(entry)

    def fetch(
        self, rule_id: str, *, limit: int = PER_RULE_LIMIT
    ) -> list[DryRunEntry]:
        """Most-recent-first."""
        with self._lock:
            bucket = self._buckets.get(rule_id)
            if bucket is None:
                return []
            entries = list(bucket)
        # Newest first.
        entries.reverse()
        if limit > 0:
            entries = entries[:limit]
        return entries

    def fetch_all(self) -> dict[str, int]:
        """Summary: ``{rule_id: count}`` of how many entries we hold
        for each rule. Useful as a status field."""
        with self._lock:
            return {rid: len(bucket) for rid, bucket in self._buckets.items()}

    def clear(self, rule_id: str | None = None) -> int:
        with self._lock:
            if rule_id is not None:
                bucket = self._buckets.pop(rule_id, None)
                return len(bucket) if bucket is not None else 0
            n = sum(len(b) for b in self._buckets.values())
            self._buckets.clear()
            return n

    def stats(self) -> dict[str, int]:
        with self._lock:
            total = sum(len(b) for b in self._buckets.values())
            return {"rules": len(self._buckets), "total_entries": total}


__all__ = ["PER_RULE_LIMIT", "TOTAL_RULES_LIMIT", "DryRunEntry", "DryRunLog"]
