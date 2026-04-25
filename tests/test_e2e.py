"""End-to-end: full daemon stack against the user's real Reqable LMDB.

Asserts the headline workflow: a Claude Code conversation calling the
MCP tools gets back coherent data describing the user's actual
captured traffic. The earlier ``test_tools_*.py`` files cover individual
tool behavior; this file exercises them in sequence the way Claude
Code would.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from reqable_mcp.daemon import Daemon, DaemonConfig
from reqable_mcp.mcp_server import set_daemon
from reqable_mcp.paths import resolve


@pytest.fixture
def live_daemon(real_lmdb_required: Path, tmp_path: Path):
    """A *real* daemon — background poller running, not the
    test-only no-thread variant. Closer to production.

    We let the background poller do the initial scan instead of
    calling scan_once ourselves; calling both racs the SQLite writer.
    """
    support = real_lmdb_required.parent
    paths = resolve(reqable_support=support, our_data=tmp_path / "data")
    d = Daemon(paths=paths, config=DaemonConfig(strict_proxy=False))
    d.start()
    set_daemon(d)

    # Wait up to 3s for the poller to insert at least one row, so
    # downstream tool calls have data to operate on.
    assert d.db is not None
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if d.db.query_recent(limit=1):
            break
        time.sleep(0.1)

    # Re-import tools so registration runs against this daemon.
    from reqable_mcp.tools import analysis, query, wait  # noqa: F401

    yield d
    d.stop()


def test_full_workflow_smoke(live_daemon: Daemon) -> None:
    """Drive a realistic chain of tool calls.

    1. ``status`` should report the daemon as live.
    2. ``list_recent`` returns at least one row (assuming the user has
       captured anything in their Reqable session).
    3. ``get_request`` on the first uid produces full metadata.
    4. ``search_url`` against the host of that uid returns ≥1 hit.
    5. ``stats`` window returns sensible counts.
    """
    from reqable_mcp.mcp_server import status as status_tool
    from reqable_mcp.tools.query import (
        get_request,
        list_recent,
        search_url,
        stats,
    )

    s = status_tool()
    assert s["started"] is True
    assert "CaptureRecordHistoryEntity" in s["schema_entities"]

    rows = list_recent(limit=10)
    if not rows:
        pytest.skip("no captured traffic available")
    first = rows[0]
    assert first["uid"]

    full = get_request(first["uid"], include_body=False, include_response_body=False)
    assert full is not None
    assert full["uid"] == first["uid"]

    if first["host"]:
        hits = search_url(first["host"], limit=3)
        assert hits, "expected to find at least one URL with our own host"

    s2 = stats(window_minutes=60)
    assert isinstance(s2["total"], int)
    assert s2["total"] >= 1


def test_query_latency_reasonable(live_daemon: Daemon) -> None:
    """Tool responses for the common query path should comfortably
    fit in the MCP per-call budget (we promise <100ms p95)."""
    from reqable_mcp.tools.query import list_recent, search_url, stats

    # Warm up
    list_recent(limit=20)

    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        list_recent(limit=20)
        times.append(time.perf_counter() - t0)

    # Generous bound — local SQLite hits should be well under this.
    assert max(times) < 0.5, f"list_recent worst case {max(times)*1000:.1f}ms exceeded budget"

    t0 = time.perf_counter()
    search_url("https", limit=10)
    assert (time.perf_counter() - t0) < 1.0

    t0 = time.perf_counter()
    stats(window_minutes=60)
    assert (time.perf_counter() - t0) < 1.0


def test_proxy_env_is_scrubbed_inside_daemon(live_daemon: Daemon) -> None:
    """Sanity for the strong proxy-loop constraint: by the time tools
    run, our process must have no proxy env vars."""
    import os

    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        assert os.environ.get(var) is None, f"{var} still set: {os.environ.get(var)!r}"
    assert os.environ.get("NO_PROXY") == "*"


def test_no_http_clients_imported() -> None:
    """Hard architectural guarantee: this codebase doesn't `import
    requests / urllib3 / aiohttp / httpx` (they'd inherit env-driven
    proxy config and break the loop guard).

    We check the source tree, not just sys.modules, because `mcp` SDK
    transitively pulls in httpx — but our own code must not."""
    import os
    import re

    src_root = Path(__file__).parent.parent / "src" / "reqable_mcp"
    pat = re.compile(
        r"^\s*(?:import|from)\s+(requests|urllib3|aiohttp|httpx)\b", re.M
    )
    offenders: list[str] = []
    for root, _, files in os.walk(src_root):
        for f in files:
            if not f.endswith(".py"):
                continue
            p = Path(root) / f
            text = p.read_text()
            if pat.search(text):
                offenders.append(str(p))
    assert not offenders, (
        f"forbidden HTTP-client imports in: {offenders}"
    )
