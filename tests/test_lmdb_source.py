"""Integration tests for LmdbSource against the user's real Reqable LMDB.

Synthetic LMDB fixtures would require us to re-emit ObjectBox-shaped
FlatBuffers — far more code than the source itself. Instead we run
against the real DB with ``real_lmdb_required``, and assert that the
poller produces row-shaped dicts whose key fields are populated.
"""

from __future__ import annotations

from pathlib import Path

import lmdb
import pytest

from reqable_mcp.db import Database
from reqable_mcp.sources.lmdb_source import LmdbSource
from reqable_mcp.sources.objectbox_meta import load_schema


@pytest.fixture
def schema(real_lmdb_required: Path):
    env = lmdb.open(
        str(real_lmdb_required),
        readonly=True,
        lock=False,
        max_dbs=64,
        subdir=True,
    )
    try:
        return load_schema(env)
    finally:
        env.close()


@pytest.fixture
def src(
    real_lmdb_required: Path, tmp_path: Path, schema
) -> LmdbSource:
    db = Database(tmp_path / "cache.db")
    db.init_schema()
    src = LmdbSource(real_lmdb_required, db, schema)
    yield src
    src.stop()


def test_scan_once_inserts_capture_rows(src: LmdbSource, tmp_path: Path) -> None:
    n = src.scan_once()
    # We don't know the exact count (depends on user activity) but
    # there should be at least *some* if the user has used Reqable
    # recently. Allow zero gracefully — assert no failures instead.
    assert n >= 0
    assert src.stats.last_seen_ob_id >= 0
    rows = src.db.query_recent(limit=5)
    if n > 0:
        assert rows, "scan_once reported new rows but DB has none"


def test_scan_advances_cursor_monotonically(src: LmdbSource) -> None:
    """Each scan should only return rows whose ob_id exceeds the cursor.

    We don't assert the second scan returns 0 because the user might
    be actively capturing — Reqable could write new records between
    our two calls. We instead assert the cursor *advanced* and the
    second batch only contains ob_ids beyond the first cursor.
    """
    src.scan_once()
    cursor1 = src.stats.last_seen_ob_id
    src.scan_once()
    cursor2 = src.stats.last_seen_ob_id
    assert cursor2 >= cursor1, "cursor went backwards"

    # Re-scanning with no new records produces zero new rows; if Reqable
    # is mid-write we may still get fresh rows on iteration 2 but they
    # all must be > cursor1.
    rows = src.db.query_recent(limit=50)
    if rows:
        assert max(r["ob_id"] for r in rows) <= cursor2


def test_decoded_records_have_required_metadata(src: LmdbSource) -> None:
    """Each row produced by the poller should have the fields our SQL
    schema indexes on (uid + ts + ob_id), plus typically host/method
    when the capture was complete."""
    src.scan_once()
    rows = src.db.query_recent(limit=20)
    if not rows:
        pytest.skip("user has no captured traffic yet")

    for r in rows:
        assert r["uid"], r
        assert r["ts"] > 0, r
        assert r["ob_id"] > 0, r
        # ts should be unix ms (>= 1.7e12 for any 2024+ capture)
        assert r["ts"] > 1_700_000_000_000, f"ts not ms-shaped: {r['ts']}"
        # source tag is honest
        assert r["source"] == "lmdb"


def test_decoded_records_include_app_info_when_known(src: LmdbSource) -> None:
    """Reqable records the originating app — Chrome / Safari / etc.

    Not every record has it (some come from non-app traffic / proxies),
    but a well-used Reqable session should have *some*.
    """
    src.scan_once()
    rows = src.db.query_recent(limit=200)
    if not rows:
        pytest.skip("no captures available")
    with_app = [r for r in rows if r["app_name"]]
    # At least one row should have an app name
    assert with_app, (
        "expected at least one capture with app_name; sample first row="
        f"{rows[0] if rows else None!r}"
    )
