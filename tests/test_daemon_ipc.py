"""End-to-end: install rule via Daemon's RuleEngine, then have a
client speak the IPC protocol the way addons.py will.

This is the seam where M11 (IPC + rules + daemon wiring) actually
matters; unit tests for each piece pass in isolation but only this
test catches a wiring mistake.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from reqable_mcp.daemon import Daemon, DaemonConfig
from reqable_mcp.ipc.protocol import PROTOCOL_VERSION, encode_message
from reqable_mcp.paths import resolve


def _round_trip(sock_path: Path, payload: dict, timeout: float = 2.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(sock_path))
    s.sendall(encode_message(payload))
    buf = b""
    while b"\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf.split(b"\n", 1)[0])


@pytest.fixture
def started_daemon(real_lmdb_required: Path, short_data_dir: Path):
    """A daemon with IPC enabled, ready to take addons-side calls."""
    support = real_lmdb_required.parent
    paths = resolve(reqable_support=support, our_data=short_data_dir)
    d = Daemon(paths=paths, config=DaemonConfig(strict_proxy=False))
    d.start()
    yield d
    d.stop()


def test_get_rules_empty_initially(started_daemon: Daemon) -> None:
    resp = _round_trip(
        started_daemon.paths.our_socket,
        {
            "v": PROTOCOL_VERSION,
            "op": "get_rules",
            "args": {
                "side": "request",
                "host": "api.example.com",
                "path": "/login",
                "method": "POST",
            },
        },
    )
    assert resp == {"ok": True, "data": []}


def test_install_rule_then_get_returns_addon_payload(started_daemon: Daemon) -> None:
    assert started_daemon.rule_engine is not None
    rule = started_daemon.rule_engine.add(
        kind="inject_header",
        side="request",
        host="api.example.com",
        payload={"name": "X-Test", "value": "1"},
    )
    resp = _round_trip(
        started_daemon.paths.our_socket,
        {
            "v": PROTOCOL_VERSION,
            "op": "get_rules",
            "args": {
                "side": "request",
                "host": "api.example.com",
                "path": "/x",
                "method": "GET",
            },
        },
    )
    assert resp["ok"] is True
    assert resp["data"] == [
        {"id": rule.id, "kind": "inject_header", "name": "X-Test", "value": "1"}
    ]


def test_get_rules_filters_by_host(started_daemon: Daemon) -> None:
    assert started_daemon.rule_engine is not None
    started_daemon.rule_engine.add(
        kind="tag",
        side="request",
        host="a.example.com",
        payload={"color": "red"},
    )
    resp = _round_trip(
        started_daemon.paths.our_socket,
        {
            "v": PROTOCOL_VERSION,
            "op": "get_rules",
            "args": {
                "side": "request",
                "host": "b.example.com",  # doesn't match
                "path": "/",
                "method": "GET",
            },
        },
    )
    assert resp == {"ok": True, "data": []}


def test_report_hit_increments_counter(started_daemon: Daemon) -> None:
    assert started_daemon.rule_engine is not None
    rule = started_daemon.rule_engine.add(
        kind="tag",
        side="request",
        payload={"color": "red"},
    )
    resp = _round_trip(
        started_daemon.paths.our_socket,
        {
            "v": PROTOCOL_VERSION,
            "op": "report_hit",
            "args": {"rule_ids": [rule.id]},
        },
    )
    assert resp == {"ok": True, "data": {}}
    # Daemon-side state mutates synchronously (handler returned ok).
    assert started_daemon.rule_engine.list_all()[0].hits == 1


def test_unknown_op_returns_error(started_daemon: Daemon) -> None:
    resp = _round_trip(
        started_daemon.paths.our_socket,
        {"v": PROTOCOL_VERSION, "op": "definitely_unknown", "args": {}},
    )
    assert resp["ok"] is False
    assert "unknown op" in resp["error"]


def test_pack_rules_drops_oversize_to_fit_frame(short_data_dir: Path) -> None:
    """A single ``replace_body`` rule large enough that *several* of
    them would exceed ``MAX_MESSAGE_BYTES`` must not crowd out the
    rest. The packer trims down to a frame the IPC server can encode.
    """
    from reqable_mcp.daemon import Daemon as DaemonCls
    from reqable_mcp.ipc.protocol import MAX_MESSAGE_BYTES, encode_message
    from reqable_mcp.rules import BODY_MAX_BYTES, RuleEngine

    engine = RuleEngine(short_data_dir / "rules.json", autoload=False)
    # Six rules at ~64 KB each = ~384 KB total, well past 256 KB cap.
    rules = []
    for i in range(6):
        rules.append(
            engine.add(
                kind="replace_body", side="response",
                host=f"h{i}.example.com",
                payload={"body": "x" * (BODY_MAX_BYTES - 10)},
            )
        )
    # match_for would normally filter — for this test we just hand the
    # full list to the packer.
    packed = DaemonCls._pack_rules_for_ipc(rules, {"host": "test"})
    # At least one was dropped (six * 64KB > budget).
    assert len(packed) < len(rules)
    # And the resulting payload encodes successfully under the cap.
    encoded = encode_message({"ok": True, "data": packed})
    assert len(encoded) <= MAX_MESSAGE_BYTES


def test_pack_rules_always_keeps_block(short_data_dir: Path) -> None:
    """A ``block`` rule must survive packing even when the rule list
    contains many large ``replace_body`` rules ahead of it. Otherwise
    the addons-side fail-open path would silently let blocked traffic
    through."""
    from reqable_mcp.daemon import Daemon as DaemonCls
    from reqable_mcp.rules import BODY_MAX_BYTES, RuleEngine

    engine = RuleEngine(short_data_dir / "rules.json", autoload=False)
    big_rules = [
        engine.add(
            kind="replace_body", side="response",
            host=f"h{i}.example.com",
            payload={"body": "x" * (BODY_MAX_BYTES - 10)},
        )
        for i in range(8)
    ]
    block = engine.add(
        kind="block", side="request",
        host="evil.example.com",
        payload={},
    )
    packed = DaemonCls._pack_rules_for_ipc(big_rules + [block], {})
    kinds = [p["kind"] for p in packed]
    assert "block" in kinds, (
        f"block rule got dropped during packing; kinds={kinds}"
    )


def test_pack_rules_passthrough_under_budget(short_data_dir: Path) -> None:
    """Small rule sets pass through the packer unchanged."""
    from reqable_mcp.daemon import Daemon as DaemonCls
    from reqable_mcp.rules import RuleEngine

    engine = RuleEngine(short_data_dir / "rules.json", autoload=False)
    r1 = engine.add(
        kind="inject_header", side="request",
        host="a.example.com",
        payload={"name": "X-A", "value": "1"},
    )
    r2 = engine.add(
        kind="tag", side="request",
        host="a.example.com",
        payload={"color": "red"},
    )
    packed = DaemonCls._pack_rules_for_ipc([r1, r2], {})
    assert len(packed) == 2
    assert {p["id"] for p in packed} == {r1.id, r2.id}


def test_relay_ipc_round_trip(started_daemon: Daemon) -> None:
    """``store_relay_value`` then ``get_relay_value`` should round-trip."""
    sock = started_daemon.paths.our_socket
    resp = _round_trip(
        sock,
        {
            "v": PROTOCOL_VERSION, "op": "store_relay_value",
            "args": {"name": "tok", "value": "abc123", "ttl_seconds": 60},
        },
    )
    assert resp == {"ok": True, "data": {"stored": True}}

    resp = _round_trip(
        sock,
        {"v": PROTOCOL_VERSION, "op": "get_relay_value", "args": {"name": "tok"}},
    )
    assert resp == {"ok": True, "data": {"value": "abc123"}}


def test_relay_get_missing_returns_null_value(started_daemon: Daemon) -> None:
    resp = _round_trip(
        started_daemon.paths.our_socket,
        {"v": PROTOCOL_VERSION, "op": "get_relay_value", "args": {"name": "nope"}},
    )
    assert resp == {"ok": True, "data": {"value": None}}


def test_relay_store_validates_inputs(started_daemon: Daemon) -> None:
    sock = started_daemon.paths.our_socket
    # missing name
    resp = _round_trip(
        sock,
        {"v": PROTOCOL_VERSION, "op": "store_relay_value",
         "args": {"value": "v", "ttl_seconds": 60}},
    )
    assert resp["ok"] is False

    # ttl out of range
    resp = _round_trip(
        sock,
        {"v": PROTOCOL_VERSION, "op": "store_relay_value",
         "args": {"name": "k", "value": "v", "ttl_seconds": 99999}},
    )
    assert resp["ok"] is False


def test_dry_run_ipc_record(started_daemon: Daemon) -> None:
    """``report_dry_run`` IPC verb stores an entry the tool can read."""
    sock = started_daemon.paths.our_socket
    resp = _round_trip(
        sock,
        {
            "v": PROTOCOL_VERSION, "op": "report_dry_run",
            "args": {
                "rule_id": "abc", "uid": "u1",
                "host": "x.example.com", "path": "/p",
                "method": "GET", "side": "request",
            },
        },
    )
    assert resp == {"ok": True, "data": {"recorded": True}}
    assert started_daemon.dry_run_log is not None
    entries = started_daemon.dry_run_log.fetch("abc")
    assert len(entries) == 1
    assert entries[0].host == "x.example.com"


def test_dry_run_ipc_validates_inputs(started_daemon: Daemon) -> None:
    sock = started_daemon.paths.our_socket
    resp = _round_trip(
        sock,
        {
            "v": PROTOCOL_VERSION, "op": "report_dry_run",
            "args": {"uid": "u"},
        },
    )
    assert resp["ok"] is False
    assert "rule_id" in resp["error"]


def test_status_reports_ipc_and_rules(started_daemon: Daemon) -> None:
    assert started_daemon.rule_engine is not None
    started_daemon.rule_engine.add(
        kind="tag",
        side="request",
        payload={"color": "red"},
    )
    # Drive one request so connection counter ticks up.
    _round_trip(
        started_daemon.paths.our_socket,
        {"v": PROTOCOL_VERSION, "op": "get_rules", "args": {"side": "request"}},
    )
    time.sleep(0.05)
    status = started_daemon.status()
    assert status["rules"]["active"] == 1
    assert status["ipc"]["connections_total"] >= 1
