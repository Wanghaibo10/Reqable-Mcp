"""Tests for DryRunLog ring buffer."""

from __future__ import annotations

from reqable_mcp.dry_run import PER_RULE_LIMIT, TOTAL_RULES_LIMIT, DryRunLog


def _record_n(log: DryRunLog, rule_id: str, n: int) -> None:
    for i in range(n):
        log.record(
            rule_id=rule_id, uid=f"u{i}", host="x", path="/p",
            method="GET", side="request",
        )


def test_record_and_fetch_newest_first() -> None:
    log = DryRunLog()
    _record_n(log, "r1", 3)
    entries = log.fetch("r1")
    assert [e.uid for e in entries] == ["u2", "u1", "u0"]


def test_per_rule_limit_overflows_oldest() -> None:
    log = DryRunLog()
    _record_n(log, "r1", PER_RULE_LIMIT + 5)
    entries = log.fetch("r1", limit=PER_RULE_LIMIT)
    # Oldest 5 rolled off.
    assert len(entries) == PER_RULE_LIMIT
    assert entries[-1].uid == "u5"  # oldest still in buffer


def test_total_rules_limit_caps_distinct() -> None:
    log = DryRunLog()
    for i in range(TOTAL_RULES_LIMIT):
        log.record(
            rule_id=f"r{i}", uid="u", host="x", path="/",
            method="GET", side="request",
        )
    # Net-new rule should be silently dropped.
    log.record(
        rule_id="overflow", uid="u", host="x", path="/",
        method="GET", side="request",
    )
    assert log.fetch("overflow") == []
    # Existing rules still recordable.
    log.record(
        rule_id="r0", uid="extra", host="x", path="/",
        method="GET", side="request",
    )
    assert any(e.uid == "extra" for e in log.fetch("r0"))


def test_fetch_unknown_returns_empty() -> None:
    log = DryRunLog()
    assert log.fetch("never-recorded") == []


def test_fetch_all_summary() -> None:
    log = DryRunLog()
    _record_n(log, "a", 3)
    _record_n(log, "b", 2)
    summary = log.fetch_all()
    assert summary == {"a": 3, "b": 2}


def test_clear_one_rule() -> None:
    log = DryRunLog()
    _record_n(log, "r1", 5)
    _record_n(log, "r2", 2)
    n = log.clear("r1")
    assert n == 5
    assert log.fetch("r1") == []
    assert len(log.fetch("r2")) == 2


def test_clear_all() -> None:
    log = DryRunLog()
    _record_n(log, "r1", 5)
    _record_n(log, "r2", 2)
    n = log.clear()
    assert n == 7
    assert log.fetch_all() == {}


def test_empty_rule_id_dropped() -> None:
    log = DryRunLog()
    log.record(rule_id="", uid="u", host="x", path="/",
               method="GET", side="request")
    assert log.fetch_all() == {}


def test_stats() -> None:
    log = DryRunLog()
    _record_n(log, "r1", 3)
    _record_n(log, "r2", 4)
    assert log.stats() == {"rules": 2, "total_entries": 7}


def test_fetch_limit_applied() -> None:
    log = DryRunLog()
    _record_n(log, "r1", 10)
    entries = log.fetch("r1", limit=3)
    assert len(entries) == 3
    # Newest first.
    assert [e.uid for e in entries] == ["u9", "u8", "u7"]
