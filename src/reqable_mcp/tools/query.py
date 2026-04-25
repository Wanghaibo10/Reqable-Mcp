"""Tier-1 query tools exposed to Claude Code via MCP.

Each ``@mcp.tool()`` function is a thin shim over Database / LmdbSource /
BodySource. Tools are kept pure: no I/O hidden in module-load time,
no caching beyond what SQLite/LMDB give us natively.

Body access strategy
--------------------
SQLite stores only metadata. When a tool needs the actual body bytes:
1. Fetch the full ``dbData`` JSON from LMDB (rare, on demand).
2. If the JSON's body section is empty (Reqable doesn't always inline
   bodies), fall back to ``capture/`` files keyed by
   ``connection.timestamp + connection.id + session.id``.
3. If neither is available, surface ``body_status="unavailable"`` —
   the metadata still helps the user.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from ..db import window_start_ms
from ..mcp_server import get_daemon, mcp
from ..sources.body_source import lookup_from_record

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- helpers


def _drop_internal_keys(row: dict[str, Any]) -> dict[str, Any]:
    """Hide columns that aren't useful to a user."""
    out = dict(row)
    out.pop("ob_id", None)
    out.pop("source", None)
    out.pop("raw_summary", None)
    return out


def _fetch_full_record(uid: str) -> tuple[dict | None, dict | None]:
    """Look up a capture by uid: (sqlite_row, lmdb_full_dbdata_json).

    Either side may be None if the record was deleted between calls.
    """
    daemon = get_daemon()
    if daemon.db is None:
        return None, None
    row = daemon.db.get_capture(uid)
    if row is None:
        return None, None
    full = (
        daemon.lmdb_source.fetch_record(int(row["ob_id"]))
        if daemon.lmdb_source is not None and row.get("ob_id")
        else None
    )
    return row, full


def _decode_body_text(payload: bytes | None) -> tuple[str, str]:
    """Best-effort UTF-8 decode of a body. Returns (text_or_b64, encoding_tag)."""
    if not payload:
        return "", "empty"
    try:
        return payload.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        import base64 as _b64

        return _b64.b64encode(payload).decode("ascii"), "base64"


# ---------------------------------------------------------------- list_recent


@mcp.tool()
def list_recent(
    limit: int = 20,
    host: str | None = None,
    method: str | None = None,
    status: int | None = None,
    app: str | None = None,
) -> list[dict[str, Any]]:
    """List recently captured requests, newest first.

    Filters are AND-combined and exact-match. Use ``host="example.com"``,
    ``method="POST"``, ``status=404``, ``app="Google Chrome"`` to narrow.
    Returns a small dict per request — call ``get_request(uid)`` for body.
    """
    daemon = get_daemon()
    if daemon.db is None:
        return []
    rows = daemon.db.query_recent(
        limit=limit, host=host, method=method, status=status, app=app
    )
    return [_drop_internal_keys(r) for r in rows]


# ---------------------------------------------------------------- get_request


@mcp.tool()
def get_request(
    uid: str,
    include_body: bool = True,
    include_response_body: bool = True,
) -> dict[str, Any] | None:
    """Get one capture in full, including headers and body.

    Body sources, in priority order:
      1. LMDB ``dbData`` JSON (if Reqable inlined it — usually for small bodies)
      2. ``capture/`` files (Reqable's on-disk body store)
      3. None (``body_status="unavailable"``)

    Set ``include_body=False`` to skip body retrieval for speed.
    """
    daemon = get_daemon()
    row, full = _fetch_full_record(uid)
    if row is None:
        return None

    out: dict[str, Any] = _drop_internal_keys(row)

    # Headers — always extracted from the LMDB JSON when available.
    if full is not None:
        sess = full.get("session") or {}
        req = sess.get("request") or {}
        res = sess.get("response") or {}
        out["request_headers"] = req.get("headers") or []
        out["response_headers"] = res.get("headers") or []
        out["origin"] = full.get("origin")
        out["app_pid"] = (full.get("appInfo") or {}).get("pid")
        out["ssl_enabled"] = bool(full.get("sslEnabled"))
    else:
        out["request_headers"] = []
        out["response_headers"] = []

    # Bodies
    bs = daemon.body_source
    if include_body or include_response_body:
        if bs is None or full is None:
            out["body_status"] = "unavailable"
        else:
            lookup = lookup_from_record(full)
            if lookup is None:
                out["body_status"] = "unavailable"
            else:
                if include_body:
                    raw = bs.get_request_body(lookup)
                    text, enc = _decode_body_text(raw)
                    out["request_body"] = text
                    out["request_body_encoding"] = enc
                if include_response_body:
                    raw = bs.get_response_body(lookup)
                    text, enc = _decode_body_text(raw)
                    out["response_body"] = text
                    out["response_body_encoding"] = enc
                out["body_status"] = "ok"
    return out


# ---------------------------------------------------------------- search_url


@mcp.tool()
def search_url(
    pattern: str,
    regex: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search captures by URL substring or regex (newest first).

    With ``regex=False`` (default) the pattern is a plain substring.
    With ``regex=True`` the pattern is a Python regex (``re.search``).
    """
    daemon = get_daemon()
    if daemon.db is None:
        return []
    rows = daemon.db.search_url(pattern, regex=regex, limit=limit)
    return [_drop_internal_keys(r) for r in rows]


# ---------------------------------------------------------------- search_body


@mcp.tool()
def search_body(
    query: str,
    target: Literal["req", "res", "both"] = "both",
    limit: int = 20,
    scan_recent: int = 200,
) -> list[dict[str, Any]]:
    """Search request/response *body* contents for ``query``.

    Body content isn't indexed (would bloat SQLite), so we scan the
    most recent ``scan_recent`` captures' bodies on demand. Increase
    ``scan_recent`` to reach further back (cost: linear scan).

    Returns matching captures with an extra ``match_target`` indicating
    where the hit was found.
    """
    daemon = get_daemon()
    if daemon.db is None or daemon.body_source is None:
        return []
    candidates = daemon.db.query_recent(limit=scan_recent)
    needle = query.lower()
    out: list[dict[str, Any]] = []
    for row in candidates:
        full = (
            daemon.lmdb_source.fetch_record(int(row["ob_id"]))
            if daemon.lmdb_source and row.get("ob_id")
            else None
        )
        if full is None:
            continue
        lookup = lookup_from_record(full)
        if lookup is None:
            continue
        hit_in: str | None = None
        if target in ("req", "both"):
            data = daemon.body_source.get_request_body(lookup)
            if data and needle in data.decode("utf-8", errors="replace").lower():
                hit_in = "req"
        if hit_in is None and target in ("res", "both"):
            data = daemon.body_source.get_response_body(lookup)
            if data and needle in data.decode("utf-8", errors="replace").lower():
                hit_in = "res"
        if hit_in:
            entry = _drop_internal_keys(row)
            entry["match_target"] = hit_in
            out.append(entry)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- to_curl


@mcp.tool()
def to_curl(uid: str, multiline: bool = True) -> str:
    """Render one capture as a runnable ``curl`` command.

    Includes ``--noproxy '*'`` so re-running the curl from the shell
    doesn't accidentally route back through Reqable's proxy and
    pollute the capture timeline.

    For multipart / large binary bodies, the body is replaced with a
    placeholder comment (the user can grab raw bytes via
    ``get_request``).
    """
    daemon = get_daemon()
    row, full = _fetch_full_record(uid)
    if row is None or full is None:
        return f"# capture {uid} not found"

    sess = full.get("session") or {}
    req = sess.get("request") or {}
    rl = req.get("requestLine") or {}
    method = (rl.get("method") or row.get("method") or "GET").upper()
    headers: list[str] = req.get("headers") or []

    url = row.get("url") or ""
    if not url:
        # Fallback: assemble from connection + path
        conn = sess.get("connection") or {}
        scheme = "https" if conn.get("security") else "http"
        host = conn.get("originHost") or row.get("host") or ""
        path = rl.get("path") or row.get("path") or "/"
        url = f"{scheme}://{host}{path}"

    sep = " \\\n  " if multiline else " "
    parts = [f"curl --noproxy '*' -X {method}"]
    for h in headers:
        # Skip pseudo-headers Reqable surfaces from h2 (e.g. ":method")
        if h.startswith(":"):
            continue
        # Single-quote escape: ' → '\''
        h_esc = h.replace("'", "'\\''")
        parts.append(f"-H '{h_esc}'")

    body = None
    if daemon.body_source is not None:
        lookup = lookup_from_record(full)
        if lookup is not None and method in ("POST", "PUT", "PATCH", "DELETE"):
            body = daemon.body_source.get_request_body(lookup)

    if body:
        try:
            body_text = body.decode("utf-8")
            body_esc = body_text.replace("'", "'\\''")
            parts.append(f"--data-raw '{body_esc}'")
        except UnicodeDecodeError:
            parts.append("# binary body omitted — fetch via get_request(uid)")

    # URL last so the command reads naturally.
    url_esc = url.replace("'", "'\\''")
    parts.append(f"'{url_esc}'")
    return sep.join(parts)


# ---------------------------------------------------------------- list_apps_seen


@mcp.tool()
def list_apps_seen(window_minutes: int = 60) -> list[dict[str, Any]]:
    """List originating apps observed in the last N minutes.

    Each entry: ``{app_name, app_id, count, last_ts}``. Useful for
    "show me everything Chrome did in the last hour" workflows.
    """
    daemon = get_daemon()
    if daemon.db is None:
        return []
    return daemon.db.list_apps_seen(since_ts_ms=window_start_ms(window_minutes))


# ---------------------------------------------------------------- stats


@mcp.tool()
def stats(window_minutes: int = 5) -> dict[str, Any]:
    """Aggregate stats over the last N minutes.

    Returns ``{total, by_host, by_method, by_status, since_ts_ms}``.
    """
    daemon = get_daemon()
    if daemon.db is None:
        return {"total": 0, "by_host": [], "by_method": [], "by_status": []}
    return daemon.db.stats(since_ts_ms=window_start_ms(window_minutes))


# ---------------------------------------------------------------- diff_requests


_DIFFABLE_FIELDS = (
    "method",
    "url",
    "host",
    "path",
    "status",
    "protocol",
    "req_mime",
    "res_mime",
    "app_name",
    "req_body_size",
    "res_body_size",
    "rtt_ms",
)


@mcp.tool()
def diff_requests(uid_a: str, uid_b: str) -> dict[str, Any]:
    """Field-level diff between two captures.

    Compares core metadata + headers (case-insensitive name match).
    Returns ``{a_only, b_only, changed, identical}`` — each section
    is a list/dict of fields where the two captures disagree.
    """
    row_a, full_a = _fetch_full_record(uid_a)
    row_b, full_b = _fetch_full_record(uid_b)
    if row_a is None or row_b is None:
        return {
            "error": "one or both uids not found",
            "uid_a_present": row_a is not None,
            "uid_b_present": row_b is not None,
        }

    changed: dict[str, dict[str, Any]] = {}
    identical: list[str] = []
    for f in _DIFFABLE_FIELDS:
        va, vb = row_a.get(f), row_b.get(f)
        if va == vb:
            identical.append(f)
        else:
            changed[f] = {"a": va, "b": vb}

    # Header diff (request side only — reasonable default)
    def _headers_dict(full: dict | None) -> dict[str, str]:
        if not full:
            return {}
        sess = full.get("session") or {}
        req = sess.get("request") or {}
        out: dict[str, str] = {}
        for h in req.get("headers") or []:
            k, _, v = h.partition(":")
            if k:
                out[k.strip().lower()] = v.strip()
        return out

    ha = _headers_dict(full_a)
    hb = _headers_dict(full_b)
    a_only = sorted(set(ha) - set(hb))
    b_only = sorted(set(hb) - set(ha))
    header_changed = {
        k: {"a": ha[k], "b": hb[k]} for k in (set(ha) & set(hb)) if ha[k] != hb[k]
    }

    return {
        "uid_a": uid_a,
        "uid_b": uid_b,
        "metadata_changed": changed,
        "metadata_identical": identical,
        "request_headers_a_only": a_only,
        "request_headers_b_only": b_only,
        "request_headers_changed": header_changed,
    }


__all__: list[str] = []  # tools are registered via the @mcp.tool decorator
