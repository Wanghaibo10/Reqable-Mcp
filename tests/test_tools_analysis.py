"""Tests for analysis tools (find_dynamic_fields / decode_jwt / extract_auth)."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from reqable_mcp.daemon import Daemon, DaemonConfig
from reqable_mcp.mcp_server import set_daemon
from reqable_mcp.paths import resolve

# ---------------------------------------------------------------- decode_jwt
# These don't need a daemon — pure decoder. Use a separate module-level
# fixture that just installs a no-op daemon for the get_daemon() call.


def _make_jwt(header: dict, payload: dict, signature: str = "fake-signature") -> str:
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    h = _b64url(json.dumps(header).encode())
    p = _b64url(json.dumps(payload).encode())
    return f"{h}.{p}.{signature}"


@pytest.fixture
def light_daemon(real_lmdb_required: Path, tmp_path: Path):
    """Minimal daemon for analysis tools that need it."""
    support = real_lmdb_required.parent
    paths = resolve(reqable_support=support, our_data=tmp_path / "data")
    os.environ.pop("REQABLE_MCP_STRICT_PROXY", None)
    d = Daemon(paths=paths, config=DaemonConfig(strict_proxy=False))

    import lmdb

    from reqable_mcp import proxy_guard
    from reqable_mcp.db import Database
    from reqable_mcp.sources.body_source import BodySource
    from reqable_mcp.sources.lmdb_source import LmdbSource
    from reqable_mcp.sources.objectbox_meta import load_schema
    from reqable_mcp.wait_queue import WaitQueue

    proxy_guard.assert_proxy_safe(strict=False)
    paths.assert_reqable_present()
    paths.ensure_our_dirs()
    env = lmdb.open(str(paths.reqable_lmdb_dir), readonly=True, lock=False, max_dbs=64, subdir=True, create=False)
    try:
        d.schema = load_schema(env)
    finally:
        env.close()
    d.db = Database(paths.our_cache_db)
    d.db.init_schema()
    d.body_source = BodySource(paths.reqable_capture_dir)
    d.wait_queue = WaitQueue()
    d.lmdb_source = LmdbSource(paths.reqable_lmdb_dir, d.db, d.schema)
    d._started = True
    d.lmdb_source.scan_once()
    set_daemon(d)
    # ensure tool modules registered
    from reqable_mcp.tools import analysis  # noqa: F401

    yield d
    d.stop()


# ---------------------------------------------------------------- decode_jwt


def test_decode_jwt_from_argument(light_daemon: Daemon) -> None:
    from reqable_mcp.tools.analysis import decode_jwt

    token = _make_jwt(
        {"alg": "HS256", "typ": "JWT"},
        {"sub": "alice", "exp": 1700000000},
        signature="sig",
    )
    out = decode_jwt(token)
    assert out["header"] == {"alg": "HS256", "typ": "JWT"}
    assert out["payload"] == {"sub": "alice", "exp": 1700000000}
    assert out["signature_b64"] == "sig"
    assert out["source"] == "argument"


def test_decode_jwt_invalid_segments(light_daemon: Daemon) -> None:
    from reqable_mcp.tools.analysis import decode_jwt

    out = decode_jwt("not.a-jwt")  # 2 segments
    assert "error" in out


def test_decode_jwt_unknown_uid(light_daemon: Daemon) -> None:
    from reqable_mcp.tools.analysis import decode_jwt

    out = decode_jwt("00000000-0000-0000-0000-000000000000")
    assert "error" in out


def test_decode_jwt_corrupt_payload(light_daemon: Daemon) -> None:
    from reqable_mcp.tools.analysis import decode_jwt

    out = decode_jwt("eyJhIjoxfQ.@@@@.sig")  # bad middle segment
    assert "error" in out


# ---------------------------------------------------------------- find_dynamic_fields


def test_find_dynamic_fields_smoke(light_daemon: Daemon) -> None:
    """Just exercise it on whatever the user has in LMDB. Don't assert
    specific dynamic fields since user traffic varies."""
    from reqable_mcp.tools.analysis import find_dynamic_fields
    from reqable_mcp.tools.query import list_recent

    rows = list_recent(limit=20)
    if not rows:
        pytest.skip("no captures available")
    host = next((r["host"] for r in rows if r["host"]), None)
    if host is None:
        pytest.skip("no host found")
    out = find_dynamic_fields(host, sample_size=10)
    assert out["host"] == host
    assert out["sample_count"] >= 0
    assert isinstance(out["dynamic"], list)
    assert isinstance(out["stable"], list)


def test_find_dynamic_fields_no_data(light_daemon: Daemon) -> None:
    from reqable_mcp.tools.analysis import find_dynamic_fields

    out = find_dynamic_fields("totally.nonexistent.local", sample_size=5)
    assert out["sample_count"] == 0
    assert out["dynamic"] == []
    assert out["stable"] == []


# ---------------------------------------------------------------- extract_auth


def test_extract_auth_smoke(light_daemon: Daemon) -> None:
    """Most user hosts have at least one auth-like header."""
    from reqable_mcp.tools.analysis import extract_auth
    from reqable_mcp.tools.query import list_recent

    rows = list_recent(limit=50)
    if not rows:
        pytest.skip("no captures available")
    # Try each host until we find one with auth headers; OK if none.
    for r in rows:
        if not r["host"]:
            continue
        out = extract_auth(r["host"], window_minutes=60)
        assert isinstance(out, list)
        if out:
            assert "uid" in out[0]
            assert "header" in out[0]
            return
    pytest.skip("no host with auth-style headers found in sample")


def test_extract_auth_unknown_host(light_daemon: Daemon) -> None:
    from reqable_mcp.tools.analysis import extract_auth

    out = extract_auth("does.not.exist.local", window_minutes=60)
    assert out == []
