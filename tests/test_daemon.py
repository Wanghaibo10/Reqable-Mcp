"""Daemon integration test.

Exercises the full start → stop cycle against the real Reqable LMDB.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from reqable_mcp.daemon import Daemon, DaemonConfig
from reqable_mcp.paths import resolve


@pytest.fixture
def daemon(real_lmdb_required: Path, short_data_dir: Path):
    # real_lmdb_required points at .../com.reqable.macosx/box; we need
    # the parent (the support dir). short_data_dir keeps the IPC socket
    # path under macOS's 104-byte AF_UNIX limit.
    support = real_lmdb_required.parent
    paths = resolve(reqable_support=support, our_data=short_data_dir)
    # Don't crash this test on user's actual proxy state.
    os.environ.pop("REQABLE_MCP_STRICT_PROXY", None)
    d = Daemon(paths=paths, config=DaemonConfig(strict_proxy=False))
    yield d
    d.stop()


def test_daemon_starts_and_loads_schema(daemon: Daemon) -> None:
    daemon.start()
    assert "CaptureRecordHistoryEntity" in daemon.schema
    assert daemon.db is not None
    assert daemon.lmdb_source is not None
    assert daemon.body_source is not None
    assert daemon.wait_queue is not None


def test_daemon_status_shape(daemon: Daemon) -> None:
    daemon.start()
    s = daemon.status()
    assert s["started"] is True
    assert s["lmdb_path"].endswith("box")
    assert s["capture_dir"].endswith("capture")
    assert s["active_waiters"] == 0
    assert "CaptureRecordHistoryEntity" in s["schema_entities"]
    # lmdb_stats might be all-zero at first scan
    assert s["lmdb_stats"] is not None


def test_daemon_start_is_idempotent(daemon: Daemon) -> None:
    daemon.start()
    initial_thread = daemon.lmdb_source._thread  # type: ignore[union-attr]
    daemon.start()  # second call should no-op, not start a new thread
    assert daemon.lmdb_source._thread is initial_thread  # type: ignore[union-attr]


def test_daemon_stop_idempotent(daemon: Daemon) -> None:
    daemon.start()
    daemon.stop()
    daemon.stop()  # should not raise


def test_daemon_lmdb_source_runs(daemon: Daemon) -> None:
    """After ~1s the poller should at least have ticked once."""
    daemon.start()
    # Wait for at least one poll cycle (default 250ms).
    time.sleep(0.6)
    assert daemon.lmdb_source is not None
    assert daemon.lmdb_source.stats.polls >= 1
