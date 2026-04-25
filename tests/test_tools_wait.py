"""Tests for the ``wait_for`` MCP tool."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from reqable_mcp.daemon import Daemon, DaemonConfig
from reqable_mcp.mcp_server import set_daemon
from reqable_mcp.paths import resolve


@pytest.fixture
def daemon(real_lmdb_required: Path, tmp_path: Path):
    support = real_lmdb_required.parent
    paths = resolve(reqable_support=support, our_data=tmp_path / "data")
    d = Daemon(paths=paths, config=DaemonConfig(strict_proxy=False))

    # Hand-init like in test_tools_query (skip lmdb_source.start so the
    # background thread can't race with manual notify in tests).
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
    env = lmdb.open(
        str(paths.reqable_lmdb_dir),
        readonly=True,
        lock=False,
        max_dbs=64,
        subdir=True,
        create=False,
    )
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

    set_daemon(d)
    from reqable_mcp.tools import wait  # noqa: F401  (registers tool)

    yield d
    d.stop()


def test_wait_for_times_out_when_no_match(daemon: Daemon) -> None:
    from reqable_mcp.tools.wait import wait_for

    t0 = time.time()
    out = wait_for(host="never.example.local", timeout_seconds=1)
    elapsed = time.time() - t0
    assert out is None
    assert 0.9 <= elapsed <= 2.0


def test_wait_for_returns_match_via_notify(daemon: Daemon) -> None:
    """Trigger wait_for in one thread, push a matching record into
    the wait queue, expect the wait to resolve."""
    from reqable_mcp.tools.wait import wait_for

    received: dict = {}

    def waiter() -> None:
        received["v"] = wait_for(host="example.com", timeout_seconds=3)

    th = threading.Thread(target=waiter)
    th.start()
    # Give wait_for a moment to register
    time.sleep(0.05)
    assert daemon.wait_queue is not None
    daemon.wait_queue.notify(
        {"uid": "x", "host": "example.com", "method": "GET", "url": "https://example.com/"}
    )
    th.join(timeout=2.0)
    assert received.get("v") is not None
    assert received["v"]["host"] == "example.com"


def test_wait_for_invalid_regex_returns_error(daemon: Daemon) -> None:
    from reqable_mcp.tools.wait import wait_for

    out = wait_for(path_pattern="(unclosed", timeout_seconds=1)
    assert isinstance(out, dict)
    assert "error" in out


def test_wait_for_timeout_capped(daemon: Daemon) -> None:
    """Don't trust huge user-supplied timeouts. We cap at 5 minutes."""
    from reqable_mcp.tools.wait import wait_for

    # We can't actually wait 5 min in a unit test; just trigger a tiny
    # match shortly after registering and verify it resolves.
    received: dict = {}

    def waiter() -> None:
        received["v"] = wait_for(
            host="example.com", timeout_seconds=10000  # huge, will be capped
        )

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.05)
    assert daemon.wait_queue is not None
    daemon.wait_queue.notify({"host": "example.com"})
    th.join(timeout=2.0)
    assert received.get("v") is not None
