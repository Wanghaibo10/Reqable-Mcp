"""Tests for the SQLite cache layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from reqable_mcp.db import Database, now_ms, window_start_ms


@pytest.fixture
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "cache.db")
    d.init_schema()
    return d


_OB_ID_COUNTER = [0]


def _next_ob_id() -> int:
    _OB_ID_COUNTER[0] += 1
    return _OB_ID_COUNTER[0]


def _sample(uid: str, *, ts: int | None = None, **overrides) -> dict:
    base = {
        "uid": uid,
        "ob_id": _next_ob_id(),
        "ts": ts if ts is not None else now_ms(),
        "scheme": "https",
        "host": "example.com",
        "port": 443,
        "url": f"https://example.com/api/{uid}",
        "path": f"/api/{uid}",
        "method": "GET",
        "status": 200,
        "protocol": "h2",
        "req_mime": None,
        "res_mime": "application/json",
        "app_name": "Chrome",
        "app_id": "com.google.Chrome",
        "app_path": "/Applications/Google Chrome.app",
        "req_body_size": 0,
        "res_body_size": 1234,
        "rtt_ms": 42,
        "comment": None,
        "ssl_bypassed": 0,
        "has_error": 0,
        "source": "lmdb",
        "raw_summary": f"GET https://example.com/api/{uid} -> 200",
    }
    base.update(overrides)
    return base


def test_init_schema_creates_tables(db: Database) -> None:
    with db.writer_connection() as c:
        rows = c.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index') "
            "ORDER BY name"
        ).fetchall()
    names = {r["name"] for r in rows}
    assert "captures" in names
    assert "captures_fts" in names
    assert "sync_state" in names
    assert "rules" in names
    assert "idx_captures_ts" in names


def test_upsert_and_get_capture(db: Database) -> None:
    rec = _sample("0e65fcea-8f3f-4244-a073-4152395ed48f")
    with db.writer_connection() as c:
        db.upsert_capture(c, rec)

    got = db.get_capture(rec["uid"])
    assert got is not None
    assert got["url"] == rec["url"]
    assert got["status"] == 200


def test_upsert_replaces_on_same_uid(db: Database) -> None:
    rec = _sample("0e65fcea-8f3f-4244-a073-4152395ed48f", status=200)
    with db.writer_connection() as c:
        db.upsert_capture(c, rec)
        rec2 = dict(rec)
        rec2["status"] = 500
        db.upsert_capture(c, rec2)

    got = db.get_capture(rec["uid"])
    assert got is not None
    assert got["status"] == 500


def test_query_recent_filters(db: Database) -> None:
    base_ts = now_ms()
    with db.writer_connection() as c:
        for i in range(5):
            db.upsert_capture(
                c,
                _sample(
                    f"00000000-0000-0000-0000-{i:012d}",
                    ts=base_ts - i * 1000,
                    method="POST" if i % 2 else "GET",
                    status=200 if i < 3 else 404,
                    host="a.com" if i < 3 else "b.com",
                    app_name="Safari" if i == 0 else "Chrome",
                ),
            )

    # Most-recent first ordering
    rows = db.query_recent(limit=10)
    assert len(rows) == 5
    assert rows[0]["ts"] >= rows[-1]["ts"]

    # Method filter
    rows = db.query_recent(method="POST", limit=10)
    assert all(r["method"] == "POST" for r in rows)

    # Status filter
    rows = db.query_recent(status=404, limit=10)
    assert len(rows) == 2

    # Host filter
    rows = db.query_recent(host="a.com", limit=10)
    assert len(rows) == 3
    assert all(r["host"] == "a.com" for r in rows)

    # App filter
    rows = db.query_recent(app="Safari", limit=10)
    assert len(rows) == 1

    # Combined
    rows = db.query_recent(host="a.com", method="GET", limit=10)
    assert {r["uid"] for r in rows} <= {r["uid"] for r in db.query_recent(host="a.com", limit=10)}


def test_search_url_substring(db: Database) -> None:
    with db.writer_connection() as c:
        db.upsert_capture(
            c,
            _sample("aa", url="https://example.com/login"),
        )
        db.upsert_capture(
            c,
            _sample("bb", url="https://example.com/api/users"),
        )
        db.upsert_capture(
            c,
            _sample("cc", url="https://other.org/login"),
        )

    rows = db.search_url("login")
    assert {r["uid"] for r in rows} == {"aa", "cc"}


def test_search_url_regex(db: Database) -> None:
    with db.writer_connection() as c:
        db.upsert_capture(c, _sample("aa", url="https://example.com/v1/users/42"))
        db.upsert_capture(c, _sample("bb", url="https://example.com/v2/users/99"))
        db.upsert_capture(c, _sample("cc", url="https://example.com/static/img.png"))

    rows = db.search_url(r"/v\d+/users/\d+", regex=True)
    assert {r["uid"] for r in rows} == {"aa", "bb"}


def test_search_summary_fts(db: Database) -> None:
    with db.writer_connection() as c:
        db.upsert_capture(
            c,
            _sample(
                "aa",
                url="https://example.com/auth",
                raw_summary="POST https://example.com/auth -> 401 unauthorized",
            ),
        )
        db.upsert_capture(
            c,
            _sample(
                "bb",
                url="https://other.org/data",
                raw_summary="GET https://other.org/data -> 200",
            ),
        )

    rows = db.search_summary_fts("unauthorized")
    assert len(rows) == 1
    assert rows[0]["uid"] == "aa"


def test_list_apps_seen(db: Database) -> None:
    base = now_ms()
    with db.writer_connection() as c:
        db.upsert_capture(c, _sample("a1", ts=base, app_name="Chrome"))
        db.upsert_capture(c, _sample("a2", ts=base, app_name="Chrome"))
        db.upsert_capture(c, _sample("a3", ts=base, app_name="Safari"))
        db.upsert_capture(c, _sample("a4", ts=base - 3_600_000, app_name="OldApp"))  # > 1h ago

    apps = db.list_apps_seen(since_ts_ms=base - 60_000)
    by_name = {a["app_name"]: a for a in apps}
    assert by_name["Chrome"]["count"] == 2
    assert by_name["Safari"]["count"] == 1
    assert "OldApp" not in by_name


def test_stats(db: Database) -> None:
    base = now_ms()
    with db.writer_connection() as c:
        for i, status in enumerate([200, 200, 404, 500]):
            db.upsert_capture(
                c,
                _sample(
                    f"x{i}",
                    ts=base,
                    status=status,
                    method="GET" if i < 2 else "POST",
                    host="a.com" if i % 2 == 0 else "b.com",
                ),
            )

    s = db.stats(since_ts_ms=base - 60_000)
    assert s["total"] == 4
    methods = {r["method"]: r["n"] for r in s["by_method"]}
    assert methods == {"GET": 2, "POST": 2}
    statuses = {r["status"]: r["n"] for r in s["by_status"]}
    assert statuses == {200: 2, 404: 1, 500: 1}


def test_sync_cursor_roundtrip(db: Database) -> None:
    assert db.get_sync_cursor("lmdb") == 0
    with db.writer_connection() as c:
        db.set_sync_cursor(c, "lmdb", last_ob_id=12345, last_ts=now_ms())
    assert db.get_sync_cursor("lmdb") == 12345


def test_window_start_ms_basic() -> None:
    ws = window_start_ms(5)
    delta = now_ms() - ws
    assert 5 * 60_000 - 50 <= delta <= 5 * 60_000 + 50
