"""Integration tests for Tier 1 query tools.

Runs against the user's real Reqable LMDB; skips on machines without one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from reqable_mcp.daemon import Daemon, DaemonConfig
from reqable_mcp.mcp_server import set_daemon
from reqable_mcp.paths import resolve


@pytest.fixture
def daemon(real_lmdb_required: Path, tmp_path: Path):
    """Daemon initialized but with the poller paused — we drive
    ``scan_once`` ourselves to avoid colliding with the background
    thread on SQLite writes during the test."""
    support = real_lmdb_required.parent
    paths = resolve(reqable_support=support, our_data=tmp_path / "data")
    os.environ.pop("REQABLE_MCP_STRICT_PROXY", None)
    d = Daemon(paths=paths, config=DaemonConfig(strict_proxy=False))

    # Re-implement Daemon.start() but skip lmdb_source.start() so the
    # background poller doesn't race with our explicit scan_once().
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

    # Synchronous scan to populate cache.db
    d.lmdb_source.scan_once()
    set_daemon(d)
    from reqable_mcp.tools import query  # noqa: F401

    yield d
    d.stop()


# ---------------------------------------------------------------- list_recent


def test_list_recent_returns_rows(daemon) -> None:
    from reqable_mcp.tools.query import list_recent

    rows = list_recent(limit=5)
    if not rows:
        pytest.skip("no captures available")
    assert len(rows) <= 5
    assert all("uid" in r for r in rows)
    # Internal columns hidden
    assert all("ob_id" not in r for r in rows)
    assert all("raw_summary" not in r for r in rows)
    # Newest first
    timestamps = [r["ts"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


def test_list_recent_filters(daemon) -> None:
    from reqable_mcp.tools.query import list_recent

    # Just exercise filter argument plumbing — actual filter results
    # depend on user data.
    rows = list_recent(limit=3, method="GET")
    for r in rows:
        assert r["method"] == "GET"


# ---------------------------------------------------------------- get_request


def test_get_request_returns_metadata(daemon) -> None:
    from reqable_mcp.tools.query import get_request, list_recent

    rows = list_recent(limit=20)
    if not rows:
        pytest.skip("no captures available")
    target = rows[0]
    full = get_request(target["uid"], include_body=False, include_response_body=False)
    assert full is not None
    assert full["uid"] == target["uid"]
    assert "request_headers" in full
    assert "response_headers" in full


def test_get_request_unknown_uid_returns_none(daemon) -> None:
    from reqable_mcp.tools.query import get_request

    assert get_request("00000000-0000-0000-0000-000000000000") is None


def test_get_request_body_status(daemon) -> None:
    """``body_status`` should be present whenever bodies are requested."""
    from reqable_mcp.tools.query import get_request, list_recent

    rows = list_recent(limit=20)
    if not rows:
        pytest.skip("no captures available")
    full = get_request(rows[0]["uid"], include_body=True, include_response_body=True)
    assert full is not None
    assert full["body_status"] in ("ok", "unavailable")


# ---------------------------------------------------------------- search_url


def test_search_url_substring(daemon) -> None:
    from reqable_mcp.tools.query import list_recent, search_url

    rows = list_recent(limit=20)
    if not rows:
        pytest.skip("no captures available")
    # Use the first row's host as substring; should at least find that.
    host = rows[0]["host"]
    if not host:
        pytest.skip("first row has no host")
    hits = search_url(host, limit=5)
    assert hits, "expected at least one URL match"
    assert all(host in (r["url"] or "") for r in hits)


def test_search_url_regex(daemon) -> None:
    from reqable_mcp.tools.query import search_url

    # Catch-all regex; should return up to limit.
    hits = search_url(r"^https?://", regex=True, limit=3)
    assert isinstance(hits, list)


# ---------------------------------------------------------------- to_curl


def test_to_curl_renders_runnable_command(daemon) -> None:
    from reqable_mcp.tools.query import list_recent, to_curl

    # Pick a non-CONNECT request (so URL is sensible).
    rows = list_recent(limit=50)
    target = next(
        (r for r in rows if r.get("method") not in (None, "CONNECT")), None
    )
    if target is None:
        pytest.skip("no non-CONNECT capture available")
    cmd = to_curl(target["uid"])
    assert cmd.startswith("curl --noproxy '*'")
    assert "-X" in cmd
    assert (target["url"] or "")[:30] in cmd or target["host"] in cmd


def test_to_curl_unknown_uid(daemon) -> None:
    from reqable_mcp.tools.query import to_curl

    out = to_curl("00000000-0000-0000-0000-000000000000")
    assert "not found" in out


# ---------------------------------------------------------------- list_apps_seen


def test_list_apps_seen(daemon) -> None:
    from reqable_mcp.tools.query import list_apps_seen

    apps = list_apps_seen(window_minutes=60)
    assert isinstance(apps, list)
    for a in apps:
        assert "app_name" in a
        assert "count" in a
        assert a["count"] > 0


# ---------------------------------------------------------------- stats


def test_stats_shape(daemon) -> None:
    from reqable_mcp.tools.query import stats

    s = stats(window_minutes=60)
    assert "total" in s
    assert "by_host" in s and isinstance(s["by_host"], list)
    assert "by_method" in s
    assert "by_status" in s


# ---------------------------------------------------------------- diff_requests


def test_diff_requests_two_real(daemon) -> None:
    from reqable_mcp.tools.query import diff_requests, list_recent

    rows = list_recent(limit=2)
    if len(rows) < 2:
        pytest.skip("need at least 2 captures to diff")
    d = diff_requests(rows[0]["uid"], rows[1]["uid"])
    assert "metadata_changed" in d
    assert "metadata_identical" in d
    assert "request_headers_changed" in d


def test_diff_requests_missing(daemon) -> None:
    from reqable_mcp.tools.query import diff_requests

    d = diff_requests("nope-1", "nope-2")
    assert d.get("error") is not None


# ---------------------------------------------------------------- search_body


def test_search_body_smoke(daemon) -> None:
    """Body search just exercises the code path; no assertion on hits
    since user traffic varies."""
    from reqable_mcp.tools.query import search_body

    out = search_body("html", target="res", limit=2, scan_recent=20)
    assert isinstance(out, list)
    assert all(r.get("match_target") in ("req", "res") for r in out)
