"""Poll Reqable's LMDB and stream new captures into our SQLite cache.

This is the primary data source. ``CaptureRecordHistoryEntity`` rows
are keyed under prefix ``\\x18\\x00\\x00\\x2c`` in the LMDB main DB.
Each value is a FlatBuffers-encoded ``Entity`` table; the ``dbData``
field is base64(gzip(JSON)) with the actual capture payload.

Polling strategy
----------------
LMDB has no native change-notification API. We poll cheap metadata
(``env.info().last_txnid``) at a small interval; only when txnid moves
do we walk the prefix range looking for new ob_id > last_seen.

Backoff: 250ms while activity is observed; expand to 1000ms / 2000ms
after consecutive idle ticks; reset on any hit.

Why not file watch (FSEvents / kqueue)?
We *could* watch ``box/data.mdb`` for mtime updates; on macOS that's
cheaper. But the LMDB file is mmap-backed and Reqable uses MDB_NOSYNC
batching — mtime updates are unreliable. txnid is authoritative.

Concurrency
-----------
The poller runs in its own daemon thread, owning a single SQLite
``writer_connection``. MCP tool readers open their own short-lived
read connections. WAL mode keeps them out of each other's way.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import lmdb

from ..db import Database
from . import flatbuffers_reader as fbr
from .objectbox_meta import Entity

log = logging.getLogger(__name__)

# All CaptureRecordHistoryEntity rows live under this 4-byte prefix.
# Determined empirically (see spec.md "ObjectBox key encoding").
CAPTURE_KEY_PREFIX = bytes.fromhex("1800002c")

# Sane upper bound to skip obviously-non-record entries when walking.
MIN_RECORD_VALUE_LEN = 32

# Backoff schedule (ms) when no new rows appear.
_IDLE_BACKOFF_MS = (250, 250, 250, 1000, 1000, 2000)


@dataclass
class LmdbSourceStats:
    """In-memory counters; surfaced via ``status`` MCP command."""

    polls: int = 0
    new_records: int = 0
    decode_failures: int = 0
    last_seen_ob_id: int = 0
    last_poll_ts_ms: int = 0


class LmdbSource:
    """Background LMDB poller.

    Parameters
    ----------
    lmdb_path:
        Path to the directory containing ``data.mdb``.
    db:
        Our SQLite cache.
    schema:
        Map name → :class:`Entity` from
        :func:`reqable_mcp.sources.objectbox_meta.load_schema`.
        Must contain ``CaptureRecordHistoryEntity``.
    on_new_capture:
        Called once per newly-decoded record with the dict that was
        upserted into SQLite. Used by the wait queue to wake waiters.
        Should be fast and non-blocking — runs on the poller thread.
    """

    def __init__(
        self,
        lmdb_path: Path,
        db: Database,
        schema: dict[str, Entity],
        on_new_capture: Callable[[dict], None] | None = None,
    ):
        self.lmdb_path = Path(lmdb_path)
        self.db = db
        self.schema = schema
        self.on_new_capture = on_new_capture or (lambda _r: None)
        self.stats = LmdbSourceStats()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._env: lmdb.Environment | None = None

        ent = schema.get("CaptureRecordHistoryEntity")
        if ent is None:
            raise RuntimeError(
                "Reqable LMDB doesn't expose CaptureRecordHistoryEntity. "
                "Either Reqable hasn't initialized its schema yet (open "
                "Reqable once and capture something), or it's running an "
                "incompatible build."
            )
        # Resolve vtable indices for the fields we read on every record.
        # Missing fields cause us to skip the record; we don't crash
        # the source over schema drift.
        vti = {p.name: p.vt_index for p in ent.properties}
        self._vt_id = vti.get("id")
        self._vt_uid = vti.get("uid")
        self._vt_timestamp = vti.get("timestamp")
        self._vt_dbdata = vti.get("dbData")
        if None in (self._vt_id, self._vt_uid, self._vt_dbdata):
            raise RuntimeError(
                f"CaptureRecordHistoryEntity is missing required fields. "
                f"Got vt_indexes: id={self._vt_id} uid={self._vt_uid} "
                f"timestamp={self._vt_timestamp} dbData={self._vt_dbdata}"
            )

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Spin up the poller thread; non-blocking."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._env = lmdb.open(
            str(self.lmdb_path),
            readonly=True,
            lock=False,
            max_dbs=64,
            subdir=True,
            create=False,
        )
        self.stats.last_seen_ob_id = self.db.get_sync_cursor("lmdb")
        self._thread = threading.Thread(
            target=self._run, name="reqable-mcp-lmdb-poller", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if self._env is not None:
            self._env.close()
            self._env = None

    def scan_once(self) -> int:
        """Run one scan synchronously, return count of new rows.

        Used by tests and by ``status`` command. The background thread
        calls this in a loop.
        """
        if self._env is None:
            self._env = lmdb.open(
                str(self.lmdb_path),
                readonly=True,
                lock=False,
                max_dbs=64,
                subdir=True,
                create=False,
            )
        return self._scan_once_unsafe()

    # ------------------------------------------------------------------ random access

    def fetch_record(self, ob_id: int) -> dict | None:
        """Fetch one capture's full ``dbData`` JSON by ObjectBox id.

        Used by ``get_request`` and ``search_body`` tools. The LMDB
        key is ``CAPTURE_KEY_PREFIX || ob_id_big_endian_u32``;
        empirically the ObjectBox ids we care about all fit in 32
        bits — for safety we still try a 64-bit form on miss.
        """
        if self._env is None:
            self._env = lmdb.open(
                str(self.lmdb_path),
                readonly=True,
                lock=False,
                max_dbs=64,
                subdir=True,
                create=False,
            )
        # Prefer 4-byte BE id (matches keys like 1800002c0018b8c7).
        candidates = [
            CAPTURE_KEY_PREFIX + ob_id.to_bytes(4, "big"),
            CAPTURE_KEY_PREFIX + ob_id.to_bytes(8, "big"),
        ]
        with self._env.begin() as txn:
            value: bytes | None = None
            for k in candidates:
                v = txn.get(k)
                if v is not None:
                    value = bytes(v)
                    break
        if value is None or len(value) < MIN_RECORD_VALUE_LEN:
            return None
        try:
            root = fbr.root_table_offset(value)
            t = fbr.parse_table(value, root)
            if self._vt_dbdata not in t.fields:
                return None
            blob = fbr.read_bytes_field(value, t.fields[self._vt_dbdata])
            if not blob:
                return None
            decoded: dict = json.loads(gzip.decompress(base64.b64decode(blob)))
            return decoded
        except Exception:
            log.exception("fetch_record decode failed for ob_id=%s", ob_id)
            return None

    # ------------------------------------------------------------------ internals

    def _run(self) -> None:
        idle_streak = 0
        last_txnid = -1
        while not self._stop.is_set():
            try:
                # Cheap read: only walk records when txnid actually moved.
                cur_txnid = self._env.info()["last_txnid"] if self._env else -1
                if cur_txnid != last_txnid:
                    n = self._scan_once_unsafe()
                    last_txnid = cur_txnid
                    if n > 0:
                        idle_streak = 0
                    else:
                        idle_streak += 1
                else:
                    idle_streak += 1
            except Exception:
                log.exception("lmdb_source poll error")
                idle_streak += 1
            self.stats.polls += 1
            self.stats.last_poll_ts_ms = int(time.time() * 1000)
            backoff_ms = _IDLE_BACKOFF_MS[min(idle_streak, len(_IDLE_BACKOFF_MS) - 1)]
            self._stop.wait(backoff_ms / 1000.0)

    def _scan_once_unsafe(self) -> int:
        """Walk the capture-record prefix and upsert records with ob_id > cursor."""
        assert self._env is not None
        cursor = self.stats.last_seen_ob_id
        new_records: list[dict] = []
        max_ob = cursor

        with self._env.begin(buffers=False) as txn:
            c = txn.cursor()
            # Walk by prefix. ObjectBox keys aren't strictly sorted by
            # ob_id, so we iterate the whole prefix range and filter.
            if not c.set_range(CAPTURE_KEY_PREFIX):
                return 0
            for k, v in c:
                if not k.startswith(CAPTURE_KEY_PREFIX):
                    break
                if len(v) < MIN_RECORD_VALUE_LEN:
                    continue
                rec = self._decode(bytes(v))
                if rec is None:
                    continue
                ob_id = rec["ob_id"]
                if ob_id <= cursor:
                    continue
                new_records.append(rec)
                if ob_id > max_ob:
                    max_ob = ob_id

        if not new_records:
            return 0

        # Bulk upsert in one transaction
        with self.db.writer_connection() as wc:
            wc.execute("BEGIN")
            for rec in new_records:
                self.db.upsert_capture(wc, rec)
            self.db.set_sync_cursor(
                wc, "lmdb", last_ob_id=max_ob, last_ts=int(time.time() * 1000)
            )
            wc.execute("COMMIT")

        self.stats.new_records += len(new_records)
        self.stats.last_seen_ob_id = max_ob
        for rec in new_records:
            try:
                self.on_new_capture(rec)
            except Exception:
                log.exception("on_new_capture callback raised")

        return len(new_records)

    # ------------------------------------------------------------------ decode

    def _decode(self, value: bytes) -> dict | None:
        """Decode one LMDB value to a SQLite-shaped dict, or None on failure."""
        try:
            root = fbr.root_table_offset(value)
            t = fbr.parse_table(value, root)
            if self._vt_id not in t.fields or self._vt_dbdata not in t.fields:
                return None
            ob_id = fbr.u64(value, t.fields[self._vt_id])
            uid = (
                fbr.read_string_field(value, t.fields[self._vt_uid])
                if self._vt_uid in t.fields
                else None
            )
            if not uid:
                return None
            ts_us = (
                fbr.u64(value, t.fields[self._vt_timestamp])
                if self._vt_timestamp is not None and self._vt_timestamp in t.fields
                else 0
            )
            blob = fbr.read_bytes_field(value, t.fields[self._vt_dbdata])
            if not blob:
                return None
            data = json.loads(gzip.decompress(base64.b64decode(blob)))
        except Exception:
            self.stats.decode_failures += 1
            return None

        return self._project_record(ob_id, uid, ts_us, data)

    @staticmethod
    def _project_record(ob_id: int, uid: str, ts_us: int, data: dict) -> dict:
        """Map the dbData JSON onto our SQLite row shape.

        Field paths confirmed against Reqable 3.0.40 captures (see
        spec.md). Missing pieces collapse to None — better to surface
        a partial row than nothing.
        """
        sess = data.get("session") or {}
        conn = sess.get("connection") or {}
        req = sess.get("request") or {}
        res = sess.get("response") or {}
        rline = req.get("requestLine") or {}
        sline = res.get("statusLine") or {}
        app = data.get("appInfo") or {}

        scheme = "https" if conn.get("security") else "http"
        host = conn.get("originHost") or ""
        port: int | None = None
        port_raw = conn.get("originPort")
        if port_raw is not None and port_raw != "":
            try:
                port = int(port_raw)
            except (TypeError, ValueError):
                port = None

        method = rline.get("method")
        path = rline.get("path") or ""
        protocol = rline.get("protocol") or sline.get("protocol")
        url = f"{scheme}://{host}{path}" if host else path

        # response timing → rtt (request start → response end)
        rtt_ms: int | None = None
        try:
            if req.get("startTimestamp") and res.get("endTimestamp"):
                rtt_us = int(res["endTimestamp"]) - int(req["startTimestamp"])
                rtt_ms = rtt_us // 1000 if rtt_us >= 0 else None
        except (TypeError, ValueError):
            pass

        status = sline.get("code")
        # Find req/res content-type from headers (case-insensitive scan).
        req_mime = _find_header(req.get("headers") or [], "content-type")
        res_mime = _find_header(res.get("headers") or [], "content-type")

        # ts: use record-level (LMDB ms) if present, else session.timestamp (us)
        ts_ms = ts_us if ts_us > 1e15 else ts_us  # LMDB stores ms-equivalent
        # Actually, the Entity-level timestamp field is unix ms; session.timestamp
        # is microseconds. We trust ts_us here as ms (Reqable uses Date type).
        # If we receive a microsecond-scale value (>1e15) treat it as us → ms.
        if ts_ms > 10**15:
            ts_ms = ts_ms // 1000

        summary = f"{method or '?'} {url[:160]} -> {status if status is not None else '?'}"

        return {
            "uid": uid,
            "ob_id": ob_id,
            "ts": ts_ms,
            "scheme": scheme if host else None,
            "host": host or None,
            "port": port,
            "url": url or None,
            "path": path or None,
            "method": method,
            "status": status,
            "protocol": protocol,
            "req_mime": req_mime,
            "res_mime": res_mime,
            "app_name": app.get("name"),
            "app_id": app.get("id"),
            "app_path": app.get("path"),
            "req_body_size": req.get("bodySize"),
            "res_body_size": res.get("bodySize"),
            "rtt_ms": rtt_ms,
            "comment": (data.get("comment") or None) or None,
            "ssl_bypassed": int(bool(data.get("sslBypassed"))),
            "has_error": int(bool(data.get("error"))),
            "source": "lmdb",
            "raw_summary": summary,
        }


def _find_header(headers: list[str], name: str) -> str | None:
    """Case-insensitive lookup in Reqable's ``["k: v", ...]`` header list."""
    needle = (name + ":").lower()
    for h in headers:
        if h.lower().startswith(needle):
            _, _, val = h.partition(":")
            return val.strip() or None
    return None


__all__ = ["CAPTURE_KEY_PREFIX", "LmdbSource", "LmdbSourceStats"]
