"""SQLite cache layer.

This is *only* an index on top of Reqable's LMDB. We never store request
or response bodies here — those are fetched on demand from LMDB ``dbData``
or from ``rest/{uid}-{req,res}.bin`` (see ``sources/rest_source.py``).

The class is intentionally low-level: it owns no threads of its own, and
each public method opens a connection / runs the query / returns Python
dicts. SQLite WAL mode lets multiple readers coexist with our single
writer thread (in ``LmdbSource``) without contention.
"""

from __future__ import annotations

import importlib.resources
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


@contextmanager
def _conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Short-lived connection. Cheap; SQLite reuses the same file."""
    c = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


class Database:
    """SQLite cache wrapper.

    Connection-per-call is fine for our scale (≤ a few hundred QPS, all
    local). The hot writer path in ``LmdbSource`` keeps a long-lived
    connection via ``writer_connection()`` to avoid prepared-statement
    re-parse overhead.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------ init

    def init_schema(self) -> None:
        ddl = importlib.resources.files("reqable_mcp").joinpath("schema.sql").read_text()
        with _conn(self.db_path) as c:
            c.executescript(ddl)

    # ------------------------------------------------------------------ writer

    @contextmanager
    def writer_connection(self) -> Iterator[sqlite3.Connection]:
        """Scoped connection for write paths.

        Used as ``with db.writer_connection() as c: ...``. The connection
        is closed on exit (the bare ``sqlite3.Connection`` context-manager
        only commits/rollbacks; it does NOT close — which would leak fds
        on every poller batch).
        """
        c = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA synchronous = NORMAL")
        c.execute("PRAGMA busy_timeout = 5000")
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def upsert_capture(self, c: sqlite3.Connection, record: dict[str, Any]) -> None:
        """Insert (or replace) one capture row.

        Caller passes its own connection so a hot loop avoids reopening.
        Uses INSERT OR REPLACE keyed on ``uid``.
        """
        cols = (
            "uid",
            "ob_id",
            "ts",
            "scheme",
            "host",
            "port",
            "url",
            "path",
            "method",
            "status",
            "protocol",
            "req_mime",
            "res_mime",
            "app_name",
            "app_id",
            "app_path",
            "req_body_size",
            "res_body_size",
            "rtt_ms",
            "comment",
            "ssl_bypassed",
            "has_error",
            "source",
            "raw_summary",
        )
        values = tuple(record.get(k) for k in cols)
        placeholders = ",".join(["?"] * len(cols))
        c.execute(
            f"INSERT OR REPLACE INTO captures ({','.join(cols)}) VALUES ({placeholders})",
            values,
        )

    # ------------------------------------------------------------------ readers

    def get_capture(self, uid: str) -> dict[str, Any] | None:
        with _conn(self.db_path) as c:
            row = c.execute("SELECT * FROM captures WHERE uid = ?", (uid,)).fetchone()
            return dict(row) if row else None

    def query_recent(
        self,
        *,
        limit: int = 20,
        host: str | None = None,
        method: str | None = None,
        status: int | None = None,
        app: str | None = None,
        since_ts_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if host is not None:
            # Reqable stores hosts lower-case; match both case variants.
            clauses.append("LOWER(host) = ?")
            params.append(host.lower())
        if method is not None:
            clauses.append("method = ?")
            params.append(method.upper())
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if app is not None:
            clauses.append("app_name = ?")
            params.append(app)
        if since_ts_ms is not None:
            clauses.append("ts >= ?")
            params.append(int(since_ts_ms))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT * FROM captures {where} ORDER BY ts DESC LIMIT ?"
        params.append(int(limit))
        with _conn(self.db_path) as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def search_url(
        self, pattern: str, *, regex: bool = False, limit: int = 20
    ) -> list[dict[str, Any]]:
        with _conn(self.db_path) as c:
            if regex:
                # SQLite's LIKE doesn't do regex; use Python-side filter for
                # the (tiny) candidate window, scanning recent rows.
                rows = c.execute(
                    "SELECT * FROM captures ORDER BY ts DESC LIMIT 5000"
                ).fetchall()
                import re as _re

                rx = _re.compile(pattern)
                out = [dict(r) for r in rows if r["url"] and rx.search(r["url"])]
                return out[:limit]
            else:
                like = f"%{pattern}%"
                return [
                    dict(r)
                    for r in c.execute(
                        "SELECT * FROM captures WHERE url LIKE ? "
                        "ORDER BY ts DESC LIMIT ?",
                        (like, int(limit)),
                    ).fetchall()
                ]

    def search_summary_fts(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search via FTS5 over (url, summary) only.

        Body search is not done here; tools.query.search_body composes a
        candidate set then drills into LMDB / rest/.
        """
        with _conn(self.db_path) as c:
            rows = c.execute(
                "SELECT captures.* FROM captures "
                "JOIN captures_fts ON captures.rowid = captures_fts.rowid "
                "WHERE captures_fts MATCH ? "
                "ORDER BY captures.ts DESC LIMIT ?",
                (query, int(limit)),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_apps_seen(self, *, since_ts_ms: int) -> list[dict[str, Any]]:
        with _conn(self.db_path) as c:
            rows = c.execute(
                "SELECT app_name, app_id, COUNT(*) AS count, MAX(ts) AS last_ts "
                "FROM captures WHERE ts >= ? AND app_name IS NOT NULL "
                "GROUP BY app_name, app_id ORDER BY count DESC",
                (since_ts_ms,),
            ).fetchall()
            return [dict(r) for r in rows]

    def stats(self, *, since_ts_ms: int) -> dict[str, Any]:
        with _conn(self.db_path) as c:
            total = c.execute(
                "SELECT COUNT(*) AS n FROM captures WHERE ts >= ?",
                (since_ts_ms,),
            ).fetchone()["n"]
            by_host = [
                dict(r)
                for r in c.execute(
                    "SELECT host, COUNT(*) AS n FROM captures "
                    "WHERE ts >= ? GROUP BY host ORDER BY n DESC LIMIT 20",
                    (since_ts_ms,),
                ).fetchall()
            ]
            by_method = [
                dict(r)
                for r in c.execute(
                    "SELECT method, COUNT(*) AS n FROM captures "
                    "WHERE ts >= ? GROUP BY method ORDER BY n DESC",
                    (since_ts_ms,),
                ).fetchall()
            ]
            by_status = [
                dict(r)
                for r in c.execute(
                    "SELECT status, COUNT(*) AS n FROM captures "
                    "WHERE ts >= ? AND status IS NOT NULL "
                    "GROUP BY status ORDER BY status",
                    (since_ts_ms,),
                ).fetchall()
            ]
            return {
                "total": total,
                "by_host": by_host,
                "by_method": by_method,
                "by_status": by_status,
                "since_ts_ms": since_ts_ms,
            }

    # ------------------------------------------------------------------ sync state

    def get_sync_cursor(self, source: str) -> int:
        with _conn(self.db_path) as c:
            row = c.execute(
                "SELECT last_ob_id FROM sync_state WHERE source = ?",
                (source,),
            ).fetchone()
            return int(row["last_ob_id"]) if row else 0

    def set_sync_cursor(
        self,
        c: sqlite3.Connection,
        source: str,
        *,
        last_ob_id: int,
        last_ts: int,
    ) -> None:
        c.execute(
            "INSERT INTO sync_state (source, last_ob_id, last_ts, last_run_ts) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(source) DO UPDATE SET "
            "  last_ob_id = excluded.last_ob_id, "
            "  last_ts    = excluded.last_ts, "
            "  last_run_ts= excluded.last_run_ts",
            (source, int(last_ob_id), int(last_ts), int(time.time() * 1000)),
        )


def now_ms() -> int:
    return int(time.time() * 1000)


def window_start_ms(window_minutes: int) -> int:
    return now_ms() - int(window_minutes) * 60_000


# Re-exports for convenience
__all__ = ["Database", "now_ms", "window_start_ms"]
