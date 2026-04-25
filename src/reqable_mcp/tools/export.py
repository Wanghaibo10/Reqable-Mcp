"""Phase 3 — body decoding, pretty-printing, and dumping to local files.

Four MCP tools share the body-fetch + decode plumbing:

* ``decode_body`` — fetch raw bytes, walk the ``Content-Encoding``
  chain (gzip / deflate / br / zstd), return decoded text.
* ``prettify``   — decode, then indent JSON / XML / HTML.
* ``dump_body``  — write the decoded body to a local file (with a
  guard against writing into Reqable's own data directory).
* ``export_har`` — write a batch of captures as HAR 1.2 to a local
  file. Compatible with Chrome DevTools / Firefox / Postman /
  Charles import.

Brotli and zstd codecs require optional dependencies — install
``reqable-mcp[export]`` to enable them. Without those packages the
respective codecs return a clean error rather than crashing.
"""

from __future__ import annotations

import base64
import datetime as _dt
import gzip
import html
import json
import logging
import os
import re  # used by _pretty_html
import zlib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse

from ..db import window_start_ms
from ..mcp_server import get_daemon, mcp
from ..sources.body_source import lookup_from_record

log = logging.getLogger(__name__)

BodySide = Literal["request", "response"]
DEFAULT_DUMP_LIMIT_BYTES: int = 16 * 1024 * 1024  # 16 MiB cap for safety

# Directories we refuse to write into. Reqable's own data must never be
# touched by us — even an accidental dump file in there could confuse
# Reqable's cleanup or be mistaken for a capture artifact.
_REFUSED_WRITE_PREFIXES: tuple[Path, ...] = (
    Path.home() / "Library" / "Application Support" / "com.reqable.macosx",
)


# ---------------------------------------------------------------- decoding


def _try_import_brotli():  # pragma: no cover - import path
    try:
        import brotli

        return brotli
    except ImportError:
        return None


def _try_import_zstd():  # pragma: no cover - import path
    try:
        import zstandard

        return zstandard
    except ImportError:
        return None


def _decode_one(data: bytes, codec: str) -> tuple[bytes | None, str | None]:
    """Apply one Content-Encoding step. Returns ``(bytes, error)``."""
    codec = codec.strip().lower()
    if codec in ("identity", ""):
        return data, None
    if codec == "gzip" or codec == "x-gzip":
        try:
            return gzip.decompress(data), None
        except OSError as e:
            return None, f"gzip decompress failed: {e}"
    if codec == "deflate":
        # Per RFC 7230, "deflate" can be either zlib-wrapped or raw —
        # try both.
        try:
            return zlib.decompress(data), None
        except zlib.error:
            try:
                return zlib.decompress(data, -zlib.MAX_WBITS), None
            except zlib.error as e:
                return None, f"deflate decompress failed: {e}"
    if codec == "br":
        brotli = _try_import_brotli()
        if brotli is None:
            return None, (
                "br codec missing — install with `pip install brotli` "
                "or `pip install 'reqable-mcp[export]'`"
            )
        try:
            return brotli.decompress(data), None
        except Exception as e:  # noqa: BLE001 - 3rd-party exc shapes vary
            return None, f"br decompress failed: {e}"
    if codec == "zstd":
        zstd = _try_import_zstd()
        if zstd is None:
            return None, (
                "zstd codec missing — install with `pip install zstandard` "
                "or `pip install 'reqable-mcp[export]'`"
            )
        try:
            return zstd.ZstdDecompressor().decompress(data), None
        except Exception as e:  # noqa: BLE001
            return None, f"zstd decompress failed: {e}"
    return None, f"unsupported codec: {codec!r}"


def _walk_content_encoding(
    raw: bytes, content_encoding: str | None
) -> tuple[bytes, list[str], str | None]:
    """Apply each codec in ``Content-Encoding`` from right to left.

    Returns ``(decoded_bytes, applied_chain, error_or_None)``. The
    chain is the list of codecs we *successfully* applied; if a codec
    fails we stop and return the partial result + an error.

    An empty body short-circuits even if a stale ``Content-Encoding``
    header is set: a 204/HEAD-style response often carries one and
    we'd rather report "no work to do" than "decompress failed".
    """
    if not content_encoding or not raw:
        return raw, [], None
    # Multiple codings comma-separated, applied last → first.
    codecs = [c.strip() for c in content_encoding.split(",") if c.strip()]
    applied: list[str] = []
    out = raw
    for codec in reversed(codecs):
        decoded, err = _decode_one(out, codec)
        if err is not None:
            return out, applied, err
        if decoded is not None:
            out = decoded
            applied.append(codec)
    return out, applied, None


def _content_encoding_from(headers: list[str]) -> str | None:
    """Find ``Content-Encoding`` (case-insensitive) in a Reqable
    header list. Returns ``None`` if absent."""
    for h in headers:
        if not h or h.startswith(":"):
            continue
        name, sep, value = h.partition(":")
        if not sep:
            continue
        if name.strip().lower() == "content-encoding":
            return value.strip()
    return None


def _content_type_from(headers: list[str]) -> str | None:
    for h in headers:
        if not h or h.startswith(":"):
            continue
        name, sep, value = h.partition(":")
        if not sep:
            continue
        if name.strip().lower() == "content-type":
            return value.strip().split(";")[0].strip().lower()
    return None


def _decode_text(payload: bytes) -> tuple[str, str]:
    """UTF-8 with base64 fallback. Mirrors query._decode_body_text."""
    if not payload:
        return "", "empty"
    try:
        return payload.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return base64.b64encode(payload).decode("ascii"), "base64"


def _fetch_raw_body(
    uid: str, side: BodySide
) -> tuple[bytes | None, str | None, str | None, dict | None]:
    """Returns ``(raw_bytes, content_encoding, content_type, full_record)``
    or ``(None, None, None, None)`` for unknown / unfetchable captures.
    """
    daemon = get_daemon()
    if daemon.db is None:
        return None, None, None, None
    row = daemon.db.get_capture(uid)
    if row is None or not row.get("ob_id"):
        return None, None, None, None
    if daemon.lmdb_source is None or daemon.body_source is None:
        return None, None, None, None
    full = daemon.lmdb_source.fetch_record(int(row["ob_id"]))
    if full is None:
        return None, None, None, None

    sess = full.get("session") or {}
    headers_block = (sess.get("request") if side == "request" else sess.get("response")) or {}
    headers = headers_block.get("headers") or []
    ce = _content_encoding_from(headers)
    ct = _content_type_from(headers)

    lookup = lookup_from_record(full)
    if lookup is None:
        return None, ce, ct, full

    if side == "request":
        raw = daemon.body_source.get_request_body(lookup)
    else:
        # We want the on-wire bytes for decode_body so the user can
        # inspect the Content-Encoding chain themselves. The
        # ``-extract`` plaintext file is for ``get_request``'s
        # convenience.
        raw = daemon.body_source.get_response_raw(lookup)
        if raw is None:
            # Fallback to whatever we can find (might be already-decoded).
            raw = daemon.body_source.get_response_body(lookup)
    return raw, ce, ct, full


# ---------------------------------------------------------------- decode_body


@mcp.tool()
def decode_body(
    uid: str, side: BodySide = "response"
) -> dict[str, Any]:
    """Fetch the body and decode its ``Content-Encoding`` chain.

    For responses we read the on-wire bytes (``-res-raw-body.reqable``)
    and walk every codec listed in ``Content-Encoding`` from right to
    left (RFC 9110 §8.4). If a codec is missing (``brotli`` /
    ``zstandard`` not installed) we surface a clean error; partial
    decodes still return what we managed to undo.

    Returns ``{decoded, decoded_encoding ('utf-8'|'base64'|'empty'),
    original_size, decoded_size, encoding_chain, content_type}`` or
    ``{error}``.
    """
    raw, ce, ct, _ = _fetch_raw_body(uid, side)
    if raw is None:
        return {"error": f"body unavailable for uid={uid!r}, side={side}"}
    decoded, applied, err = _walk_content_encoding(raw, ce)
    text, enc = _decode_text(decoded)
    out: dict[str, Any] = {
        "decoded": text,
        "decoded_encoding": enc,
        "original_size": len(raw),
        "decoded_size": len(decoded),
        "encoding_chain": applied,
        "content_type": ct,
    }
    if err is not None:
        out["error"] = err
    return out


# ---------------------------------------------------------------- prettify


_FormatHint = Literal["json", "xml", "html", "auto"]

def _detect_format(content_type: str | None, sample: str) -> str:
    """Pick a formatter for ``prettify``.

    Strategy: trust ``Content-Type`` if it names json/xml/html; otherwise
    sniff the leading bytes. HTML beats XML when ``<!doctype html`` or
    ``<html`` is in the first 256 chars (``<!doctype xxx`` for non-HTML
    document types still falls through to xml).
    """
    if content_type:
        if "json" in content_type:
            return "json"
        if "xml" in content_type:
            return "xml"
        if "html" in content_type:
            return "html"
    s = sample.lstrip()
    if not s:
        return "text"
    if s[0] in "{[":
        return "json"
    head = s[:256].lower()
    if "<!doctype html" in head or "<html" in head:
        return "html"
    if s.startswith("<?xml") or (
        s.startswith("<") and len(s) > 1 and (s[1].isalpha() or s[1] == "!")
    ):
        return "xml"
    return "text"


def _pretty_json(text: str) -> tuple[str, str | None]:
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False), None
    except (ValueError, TypeError) as e:
        return text, f"json parse failed: {e}"


def _pretty_xml(text: str) -> tuple[str, str | None]:
    try:
        from xml.dom.minidom import (
            parseString,  # noqa: S408 — input is captured traffic, not user-supplied.
        )

        return parseString(text).toprettyxml(indent="  "), None  # noqa: S318
    except Exception as e:  # noqa: BLE001 - parser exc shapes vary
        return text, f"xml parse failed: {e}"


def _pretty_html(text: str) -> tuple[str, str | None]:
    """Lightweight HTML pretty-print using stdlib only.

    We don't pull in BeautifulSoup; this is a pragmatic indenter that
    inserts newlines around block-level tags. Good enough for skim
    reading; not a full DOM round-trip.
    """
    # Decode HTML entities once so the user reads &quot; as ".
    decoded = html.unescape(text)
    # Insert newlines around tag boundaries; collapse repeats.
    out = re.sub(r">\s*<", ">\n<", decoded)
    return out.strip(), None


@mcp.tool()
def prettify(
    uid: str,
    side: BodySide = "response",
    format: _FormatHint = "auto",
) -> dict[str, Any]:
    """Decode + pretty-print a body.

    ``format="auto"`` picks JSON / XML / HTML based on Content-Type
    (and a content sniff fallback). Pass ``format="json"`` etc. to
    force a specific formatter.

    Returns ``{pretty, format, content_type, encoding_chain, error?}``.
    """
    raw, ce, ct, _ = _fetch_raw_body(uid, side)
    if raw is None:
        return {"error": f"body unavailable for uid={uid!r}, side={side}"}
    decoded, applied, decode_err = _walk_content_encoding(raw, ce)
    text, text_enc = _decode_text(decoded)
    if text_enc == "base64":
        return {
            "error": "body is binary; prettify cannot format base64",
            "content_type": ct,
            "encoding_chain": applied,
        }

    chosen = format if format != "auto" else _detect_format(ct, text)
    if chosen == "json":
        pretty, err = _pretty_json(text)
    elif chosen == "xml":
        pretty, err = _pretty_xml(text)
    elif chosen == "html":
        pretty, err = _pretty_html(text)
    else:
        pretty, err = text, None

    out: dict[str, Any] = {
        "pretty": pretty,
        "format": chosen,
        "content_type": ct,
        "encoding_chain": applied,
    }
    if decode_err is not None:
        out["decode_error"] = decode_err
    if err is not None:
        out["format_error"] = err
    return out


# ---------------------------------------------------------------- dump_body


def _validate_dump_path(path: str) -> tuple[Path | None, str | None]:
    """Reject relative paths and writes into Reqable's data directory."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        return None, f"path must be absolute, got {path!r}"
    try:
        resolved = p.resolve(strict=False)
    except OSError as e:
        return None, f"cannot resolve path: {e}"
    for refused in _REFUSED_WRITE_PREFIXES:
        try:
            resolved.relative_to(refused.resolve(strict=False))
        except ValueError:
            continue
        return None, (
            f"refusing to write under {refused} — that's Reqable's own "
            "data directory; pick another location"
        )
    return resolved, None


@mcp.tool()
def dump_body(
    uid: str,
    side: BodySide,
    path: str,
    decoded: bool = True,
) -> dict[str, Any]:
    """Write a captured body to a local file.

    ``decoded=True`` (default) writes after walking ``Content-Encoding``
    so the file on disk is the plaintext payload. ``decoded=False``
    writes the on-wire bytes verbatim (still gzip / br / zstd / etc.).

    ``path`` must be absolute. Writes under Reqable's own data
    directory are refused — that's not our space to touch.

    Returns ``{path, size, encoding_chain, content_type}`` or ``{error}``.
    """
    target, err = _validate_dump_path(path)
    if err is not None:
        return {"error": err}
    raw, ce, ct, _ = _fetch_raw_body(uid, side)
    if raw is None:
        return {"error": f"body unavailable for uid={uid!r}, side={side}"}

    if decoded and ce:
        body, applied, decode_err = _walk_content_encoding(raw, ce)
    else:
        body, applied, decode_err = raw, [], None

    if len(body) > DEFAULT_DUMP_LIMIT_BYTES:
        return {
            "error": (
                f"body size {len(body)} exceeds dump limit "
                f"{DEFAULT_DUMP_LIMIT_BYTES}"
            )
        }

    assert target is not None  # _validate_dump_path returned no error

    # Open the *raw* (un-resolved) path so ``O_NOFOLLOW`` actually has
    # a symlink to refuse. ``target`` from _validate_dump_path went
    # through ``resolve(strict=False)`` which already followed any
    # symlink in the input — using it for the write would defeat the
    # TOCTOU defense. The Reqable-dir refusal already used the
    # resolved form.
    raw_target = Path(path).expanduser()
    raw_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(str(raw_target), flags, 0o600)
    except OSError as e:
        return {"error": f"open refused (symlink? {e})"}
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
    except OSError as e:
        return {"error": f"write failed: {e}"}

    out: dict[str, Any] = {
        "path": str(target),
        "size": len(body),
        "encoding_chain": applied,
        "content_type": ct,
    }
    if decode_err is not None:
        out["decode_error"] = decode_err
    return out


# ---------------------------------------------------------------- export_har
#
# HAR 1.2 spec: https://w3c.github.io/web-performance/specs/HAR/Overview.html
#
# We emit the minimum-viable subset that DevTools / Charles / Postman
# accept:
#   log.version, log.creator, log.entries[]
#   entries[].startedDateTime, .time, .request, .response, .cache, .timings
# Cookies, queryString, postData are filled when we can extract them
# cheaply from the captured headers / body.

_HAR_HARDCAP_ENTRIES: int = 10_000
_HAR_DEFAULT_LIMIT: int = 1_000

# Standard HTTP status text — used as fallback when Reqable doesn't
# surface ``message`` (it usually does for 1.x but not for h2/h3).
_STATUS_TEXT: dict[int, str] = {
    100: "Continue", 101: "Switching Protocols",
    200: "OK", 201: "Created", 202: "Accepted", 204: "No Content",
    206: "Partial Content",
    301: "Moved Permanently", 302: "Found", 303: "See Other",
    304: "Not Modified", 307: "Temporary Redirect", 308: "Permanent Redirect",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden",
    404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
    409: "Conflict", 410: "Gone", 415: "Unsupported Media Type",
    422: "Unprocessable Entity", 429: "Too Many Requests",
    500: "Internal Server Error", 501: "Not Implemented",
    502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout",
}


def _name_value_pairs(headers: list[str]) -> list[dict[str, str]]:
    """Reqable header strings ``"name: value"`` → HAR ``[{name, value}]``.
    Pseudo-headers (``:method`` etc.) are dropped."""
    out: list[dict[str, str]] = []
    for h in headers or []:
        if not h or h.startswith(":"):
            continue
        name, sep, value = h.partition(":")
        if not sep:
            continue
        out.append({"name": name.strip(), "value": value.strip()})
    return out


def _ts_to_iso(ts_ms: int | None) -> str:
    """Reqable stores ts in unix-ms. HAR wants ISO 8601 in UTC."""
    if ts_ms is None:
        return "1970-01-01T00:00:00.000Z"
    return (
        _dt.datetime.fromtimestamp(ts_ms / 1000, tz=_dt.timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.")
        + f"{ts_ms % 1000:03d}Z"
    )


def _capture_to_har_entry(uid: str) -> dict[str, Any] | None:
    """Build one HAR entry for a capture. Returns None if missing."""
    daemon = get_daemon()
    if daemon.db is None:
        return None
    row = daemon.db.get_capture(uid)
    if row is None:
        return None
    full = (
        daemon.lmdb_source.fetch_record(int(row["ob_id"]))
        if daemon.lmdb_source is not None and row.get("ob_id")
        else None
    )

    sess = (full or {}).get("session") or {}
    cap_req = sess.get("request") or {}
    cap_res = sess.get("response") or {}
    rl = cap_req.get("requestLine") or {}

    method = (rl.get("method") or row.get("method") or "GET").upper()
    url = row.get("url") or ""
    if not url:
        conn = sess.get("connection") or {}
        scheme = "https" if conn.get("security") else "http"
        host = conn.get("originHost") or row.get("host") or ""
        if not host:
            # Without a host we'd emit an invalid URL Chrome HAR
            # import would reject. Skip this entry.
            return None
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"  # IPv6 literal must be bracketed
        path = rl.get("path") or row.get("path") or "/"
        url = f"{scheme}://{host}{path}"

    parsed = urlparse(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    http_version = (cap_req.get("protocol") or row.get("protocol") or "HTTP/1.1").upper()
    if http_version == "H2":
        http_version = "HTTP/2"
    elif http_version == "H3":
        http_version = "HTTP/3"

    # Body: prefer decoded plaintext; if binary, base64. We don't dump
    # request bodies into HAR for non-body methods (GET/HEAD/OPTIONS).
    req_post: dict[str, Any] | None = None
    if (
        method in ("POST", "PUT", "PATCH", "DELETE")
        and full is not None
        and daemon.body_source is not None
    ):
        lookup = lookup_from_record(full)
        if lookup is not None:
            raw = daemon.body_source.get_request_body(lookup)
            if raw:
                # Try Content-Encoding decode; fall back to raw.
                ce = _content_encoding_from(cap_req.get("headers") or [])
                if ce:
                    decoded, _, _ = _walk_content_encoding(raw, ce)
                else:
                    decoded = raw
                ct = _content_type_from(cap_req.get("headers") or []) or "application/octet-stream"
                text, enc = _decode_text(decoded)
                if enc == "base64":
                    req_post = {"mimeType": ct, "text": text, "encoding": "base64"}
                else:
                    req_post = {"mimeType": ct, "text": text}

    # Response body
    res_content: dict[str, Any] = {
        "size": row.get("res_body_size") or -1,
        "mimeType": row.get("res_mime") or "",
    }
    if full is not None and daemon.body_source is not None:
        lookup = lookup_from_record(full)
        if lookup is not None:
            raw = daemon.body_source.get_response_body(lookup, prefer_decoded=True)
            if raw:
                text, enc = _decode_text(raw)
                if enc == "base64":
                    res_content["text"] = text
                    res_content["encoding"] = "base64"
                else:
                    res_content["text"] = text

    # redirectURL: pull Location header from response if 3xx
    redirect_url = ""
    status = row.get("status") or 0
    if 300 <= status < 400:
        for h in cap_res.get("headers") or []:
            kv = h.partition(":")
            if kv[0].strip().lower() == "location" and kv[1]:
                redirect_url = kv[2].strip()
                break

    rtt = row.get("rtt_ms") or 0
    status_message = (
        cap_res.get("message") or _STATUS_TEXT.get(status, "")
    )
    ts_ms = row.get("ts")
    started_iso = _ts_to_iso(ts_ms)

    return {
        "startedDateTime": started_iso,
        "time": rtt,
        "request": {
            "method": method,
            "url": url,
            "httpVersion": http_version,
            "cookies": [],
            "headers": _name_value_pairs(cap_req.get("headers") or []),
            "queryString": [{"name": k, "value": v} for k, v in query_pairs],
            "headersSize": -1,
            "bodySize": row.get("req_body_size") or 0,
            **({"postData": req_post} if req_post is not None else {}),
        },
        "response": {
            "status": status,
            "statusText": status_message,
            "httpVersion": http_version,
            "cookies": [],
            "headers": _name_value_pairs(cap_res.get("headers") or []),
            "content": res_content,
            "redirectURL": redirect_url,
            "headersSize": -1,
            "bodySize": row.get("res_body_size") or -1,
        },
        "cache": {},
        "timings": {
            "send": 0,
            "wait": rtt,
            "receive": 0,
        },
        "_reqable_uid": uid,
        "_app_name": row.get("app_name"),
    }


@mcp.tool()
def export_har(
    path: str,
    uids: list[str] | None = None,
    host: str | None = None,
    window_minutes: int | None = None,
    limit: int = _HAR_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Export captures to a HAR 1.2 file (Chrome DevTools / Postman / Charles).

    At least one selector must be specified — refusing the unfiltered
    case prevents accidentally writing every capture in history. The
    selectors compose:

    * ``uids`` — explicit list. Wins over the other filters when given.
    * ``host`` + ``window_minutes`` — pull recent captures matching
      both. ``window_minutes`` alone exports everything in the window.
    * ``host`` alone — last ``limit`` captures for that host.

    Output cap: ``limit`` (default 1000, hardcap 10000). Path must be
    absolute and not under Reqable's own data directory.

    Returns ``{path, entry_count, skipped_count, size}`` or ``{error}``.
    """
    if not (uids or host or window_minutes):
        return {
            "error": (
                "specify at least one of uids / host / window_minutes — "
                "refusing to export every capture in the database"
            )
        }
    if limit <= 0 or limit > _HAR_HARDCAP_ENTRIES:
        return {
            "error": (
                f"limit must be in (0, {_HAR_HARDCAP_ENTRIES}], got {limit}"
            )
        }

    target, err = _validate_dump_path(path)
    if err is not None:
        return {"error": err}
    assert target is not None

    daemon = get_daemon()
    if daemon.db is None:
        return {"error": "database unavailable"}

    if uids:
        # Explicit list — preserve caller order, cap at limit.
        target_uids: list[str] = list(uids)[:limit]
    else:
        rows = daemon.db.query_recent(
            limit=limit,
            host=host,
            since_ts_ms=(window_start_ms(window_minutes) if window_minutes else None),
        )
        target_uids = [r["uid"] for r in rows]

    entries: list[dict[str, Any]] = []
    skipped = 0
    for u in target_uids:
        entry = _capture_to_har_entry(u)
        if entry is None:
            skipped += 1
            continue
        entries.append(entry)

    har = {
        "log": {
            "version": "1.2",
            "creator": {"name": "reqable-mcp", "version": "0.1.0a1"},
            "entries": entries,
        }
    }

    # Same O_NOFOLLOW defense as ``dump_body``. Use the raw path
    # because the resolved form has already followed symlinks.
    raw_target = Path(path).expanduser()
    raw_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(str(raw_target), flags, 0o600)
    except OSError as e:
        return {"error": f"open refused (symlink? {e})"}
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(har, f, ensure_ascii=False, indent=2)
        size = raw_target.stat().st_size
    except OSError as e:
        return {"error": f"write failed: {e}"}

    return {
        "path": str(target),
        "entry_count": len(entries),
        "skipped_count": skipped,
        "size": size,
    }


# ---------------------------------------------------------------- export_mitmproxy_flow


def _try_import_mitmproxy():  # pragma: no cover - import path
    try:
        from mitmproxy import http as _http
        from mitmproxy import io as _io
        from mitmproxy.connection import Client as _Client
        from mitmproxy.connection import Server as _Server
    except ImportError:
        return None
    return {"http": _http, "io": _io, "Client": _Client, "Server": _Server}


@mcp.tool()
def export_mitmproxy_flow(
    path: str,
    uids: list[str] | None = None,
    host: str | None = None,
    window_minutes: int | None = None,
    limit: int = _HAR_DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Export captures to a mitmproxy ``.flow`` file.

    The output is the binary tnetstring format mitmweb / mitmdump
    natively read with ``--rfile``. Useful for replaying captures
    through mitmproxy's scripting layer.

    Selectors compose the same way as :func:`export_har`: at least
    one of ``uids`` / ``host`` / ``window_minutes`` must be set.
    Path must be absolute and not under Reqable's data dir.

    Requires the ``mitmproxy`` package — install with
    ``pip install 'reqable-mcp[mitmproxy]'``.

    Returns ``{path, flow_count, skipped_count, size}`` or ``{error}``.
    """
    mods = _try_import_mitmproxy()
    if mods is None:
        return {
            "error": (
                "mitmproxy package not installed; "
                "run `pip install 'reqable-mcp[mitmproxy]'`"
            )
        }

    if not (uids or host or window_minutes):
        return {
            "error": (
                "specify at least one of uids / host / window_minutes — "
                "refusing to export every capture in the database"
            )
        }
    if limit <= 0 or limit > _HAR_HARDCAP_ENTRIES:
        return {
            "error": (
                f"limit must be in (0, {_HAR_HARDCAP_ENTRIES}], got {limit}"
            )
        }

    target, err = _validate_dump_path(path)
    if err is not None:
        return {"error": err}
    assert target is not None

    daemon = get_daemon()
    if daemon.db is None:
        return {"error": "database unavailable"}

    if uids:
        target_uids: list[str] = list(uids)[:limit]
    else:
        from ..db import window_start_ms as _wsm
        rows = daemon.db.query_recent(
            limit=limit,
            host=host,
            since_ts_ms=(_wsm(window_minutes) if window_minutes else None),
        )
        target_uids = [r["uid"] for r in rows]

    flows: list[Any] = []
    skipped = 0
    for uid in target_uids:
        flow = _capture_to_mitmproxy_flow(uid, mods)
        if flow is None:
            skipped += 1
            continue
        flows.append(flow)

    raw_target = Path(path).expanduser()
    raw_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(str(raw_target), flags, 0o600)
    except OSError as e:
        return {"error": f"open refused (symlink? {e})"}
    try:
        with os.fdopen(fd, "wb") as f:
            writer = mods["io"].FlowWriter(f)
            for flow in flows:
                writer.add(flow)
        size = raw_target.stat().st_size
    except OSError as e:
        return {"error": f"write failed: {e}"}

    return {
        "path": str(target),
        "flow_count": len(flows),
        "skipped_count": skipped,
        "size": size,
    }


def _capture_to_mitmproxy_flow(uid: str, mods: dict) -> Any | None:
    """Build one ``mitmproxy.http.HTTPFlow`` from a capture, or None
    when the capture can't be reconstructed."""
    import time as _time

    daemon = get_daemon()
    if daemon.db is None:
        return None
    row = daemon.db.get_capture(uid)
    if row is None:
        return None
    full = (
        daemon.lmdb_source.fetch_record(int(row["ob_id"]))
        if daemon.lmdb_source is not None and row.get("ob_id")
        else None
    )

    sess = (full or {}).get("session") or {}
    cap_req = sess.get("request") or {}
    cap_res = sess.get("response") or {}
    rl = cap_req.get("requestLine") or {}

    method = (rl.get("method") or row.get("method") or "GET").upper()
    url = row.get("url") or ""
    if not url:
        conn = sess.get("connection") or {}
        scheme = "https" if conn.get("security") else "http"
        host = conn.get("originHost") or row.get("host") or ""
        if not host:
            return None
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        path = rl.get("path") or row.get("path") or "/"
        url = f"{scheme}://{host}{path}"

    # Headers list[str] → list[(bytes, bytes)] tuples — mitmproxy's
    # Request.make / Response.make take bytes pairs.
    # Drop pseudo-headers (h2 ``:method`` / ``:status`` etc.).
    def _to_pairs(headers: list[str]) -> list[tuple[bytes, bytes]]:
        pairs: list[tuple[bytes, bytes]] = []
        for h in headers or []:
            if not h or h.startswith(":"):
                continue
            name, sep, value = h.partition(":")
            if not sep:
                continue
            try:
                pairs.append(
                    (name.strip().encode("latin-1"),
                     value.strip().encode("latin-1"))
                )
            except UnicodeEncodeError:
                # Header outside HTTP-supported charset — skip it.
                continue
        return pairs

    # Bodies — best-effort. mitmproxy's Request.make/Response.make
    # take bytes for the body.
    req_body = b""
    res_body = b""
    if full is not None and daemon.body_source is not None:
        lookup = lookup_from_record(full)
        if lookup is not None:
            req_body = daemon.body_source.get_request_body(lookup) or b""
            res_body = daemon.body_source.get_response_body(
                lookup, prefer_decoded=True
            ) or b""

    # noqa N806 throughout — these are class names rebound from a
    # dict, not local variables in the conventional sense.
    Client = mods["Client"]  # noqa: N806
    Server = mods["Server"]  # noqa: N806
    Request = mods["http"].Request  # noqa: N806
    Response = mods["http"].Response  # noqa: N806
    HTTPFlow = mods["http"].HTTPFlow  # noqa: N806

    parsed_host = ""
    parsed_port = 443
    try:
        from urllib.parse import urlparse as _urlparse
        p = _urlparse(url)
        parsed_host = p.hostname or ""
        parsed_port = p.port or (443 if p.scheme == "https" else 80)
    except Exception:  # noqa: BLE001
        pass

    now = _time.time()
    client = Client(
        peername=("127.0.0.1", 0),
        sockname=("127.0.0.1", 0),
        timestamp_start=now,
    )
    server = Server(address=(parsed_host or "unknown", parsed_port))
    server.timestamp_start = now

    flow = HTTPFlow(client, server)
    flow.request = Request.make(method, url, req_body, _to_pairs(cap_req.get("headers") or []))
    status = row.get("status") or 0
    if status:
        flow.response = Response.make(status, res_body, _to_pairs(cap_res.get("headers") or []))
    return flow


__all__: list[str] = []  # tools register via @mcp.tool
