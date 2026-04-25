"""Phase 3 — replay a captured request, optionally with overrides.

The natural next step after ``get_request`` / ``diff_requests``: an LLM
spotted a request that failed, wants to tweak a header / body, and
re-issue it. This tool is the seam.

Hard rules:

* **Proxy bypass.** We deliberately use stdlib ``urllib.request`` with
  an empty :class:`ProxyHandler` so the replay does NOT route back
  through Reqable's MITM and pollute the capture timeline. Daemon
  startup also sets ``NO_PROXY=*`` (see :mod:`proxy_guard`); the
  explicit handler is belt-and-suspenders.
* **Read-only.** We never write LMDB or SQLite from a replay. The
  caller gets the new response back; nothing is persisted.
* **TLS verification on.** We use ``ssl.create_default_context()`` —
  no skip-verify shortcut. Replaying against a server with a broken
  cert is a real failure mode the user should see, not paper over.
"""

from __future__ import annotations

import base64
import http.client
import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from ..mcp_server import get_daemon, mcp
from ..rules import BODY_MAX_BYTES
from ..sources.body_source import lookup_from_record

log = logging.getLogger(__name__)

# Hard cap. ``timeout=0`` is meaningless for a replay; large timeouts
# can pin a worker for ages if the upstream hangs.
TIMEOUT_MIN_S: float = 0.1
TIMEOUT_MAX_S: float = 60.0


def _split_header(line: str) -> tuple[str, str] | None:
    """Reqable surfaces headers as ``"name: value"`` strings.

    Returns ``None`` for pseudo-headers (h2 ``:method`` etc.) and
    structurally broken lines.
    """
    if not line or line.startswith(":"):
        return None
    name, sep, value = line.partition(":")
    if not sep:
        return None
    return name.strip(), value.strip()


def _ci_dict(headers: list[str]) -> dict[str, str]:
    """Build a case-insensitive (lowercased keys) dict from header list."""
    out: dict[str, str] = {}
    for h in headers:
        kv = _split_header(h)
        if kv is None:
            continue
        out[kv[0].lower()] = kv[1]
    return out


def _merge_headers(
    base: list[str], overrides: dict[str, str] | None
) -> list[tuple[str, str]]:
    """Apply user overrides to captured headers.

    * Empty-string override deletes the header.
    * Otherwise the override replaces the captured value
      (case-insensitive on header name).
    * Headers absent from the capture are appended.
    * Pseudo-headers (``:method`` / ``:path`` / etc.) and
      ``Content-Length`` (we'll re-set it) are dropped.
    """
    captured = _ci_dict(base)
    captured.pop("content-length", None)

    if overrides:
        for k, v in overrides.items():
            if not isinstance(k, str) or not k:
                continue
            key = k.lower()
            # Content-Length is computed by urllib from the actual
            # body bytes. Letting a caller set it would just create
            # a body / declared-length mismatch — silently drop it.
            if key == "content-length":
                continue
            if v == "":
                captured.pop(key, None)
            else:
                captured[key] = v

    # Preserve the captured header casing where we can; otherwise
    # use the override's casing.
    casing: dict[str, str] = {}
    for h in base:
        kv = _split_header(h)
        if kv is not None:
            casing[kv[0].lower()] = kv[0]
    if overrides:
        for k in overrides:
            if isinstance(k, str) and k.lower() not in casing:
                casing[k.lower()] = k

    return [(casing.get(k, k), v) for k, v in captured.items()]


def _coerce_body(body: Any) -> tuple[bytes | None, str | None, str | None]:
    """Returns ``(bytes, content_type_hint, error)``.

    * ``None`` → no body (caller decides whether to use captured body)
    * ``""``   → explicit empty body
    * ``str``  → encode UTF-8
    * ``dict`` → json.dumps + Content-Type hint

    Other types are rejected.
    """
    if body is None:
        return None, None, None
    if isinstance(body, str):
        try:
            data = body.encode("utf-8")
        except UnicodeEncodeError as e:
            return None, None, f"body string not UTF-8 encodable: {e}"
        if len(data) > BODY_MAX_BYTES:
            return None, None, f"body exceeds BODY_MAX_BYTES={BODY_MAX_BYTES}"
        return data, None, None
    if isinstance(body, dict):
        try:
            text = json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            return None, None, f"body dict not JSON-serializable: {e}"
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError as e:
            return None, None, f"body dict not UTF-8 encodable: {e}"
        if len(data) > BODY_MAX_BYTES:
            return None, None, f"body exceeds BODY_MAX_BYTES={BODY_MAX_BYTES}"
        return data, "application/json", None
    return None, None, (
        f"body must be str, dict, or None; got {type(body).__name__}. "
        "Binary bodies are not supported in replay."
    )


def _decode_body(payload: bytes) -> tuple[str, str]:
    """UTF-8 with base64 fallback. Mirrors query._decode_body_text."""
    if not payload:
        return "", "empty"
    try:
        return payload.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return base64.b64encode(payload).decode("ascii"), "base64"


# ---------------------------------------------------------------- replay_request


@mcp.tool()
def replay_request(
    uid: str,
    method: str | None = None,
    url: str | None = None,
    headers: dict[str, str] | None = None,
    body: str | dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Re-issue a captured request, optionally with overrides.

    Reads the captured request from LMDB, applies any overrides, then
    re-sends it with **all proxies bypassed** so the replay does NOT
    route back through Reqable's MITM and pollute the capture timeline.
    Nothing is written to LMDB / SQLite.

    Override semantics:

    * ``method`` — uppercase HTTP verb, or ``None`` to keep captured.
    * ``url`` — full replacement URL (scheme + host + path + query).
    * ``headers`` — case-insensitive merge over captured headers. Pass
      ``""`` as a value to *delete* a header. ``Content-Length`` is
      always recomputed.
    * ``body`` — ``None`` keeps the captured body; ``""`` clears it;
      ``str`` is sent as-is; ``dict`` is ``json.dumps``-ed and
      ``Content-Type: application/json`` is auto-added if absent.

    Returns ``{status, status_message, headers, response_body,
    response_body_encoding, rtt_ms, url_actual}`` on success, or
    ``{error}`` on validation / network failure.
    """
    if not (TIMEOUT_MIN_S <= timeout_seconds <= TIMEOUT_MAX_S):
        return {
            "error": (
                f"timeout_seconds must be in [{TIMEOUT_MIN_S}, "
                f"{TIMEOUT_MAX_S}], got {timeout_seconds}"
            )
        }

    daemon = get_daemon()
    if daemon.db is None:
        return {"error": "database unavailable"}
    row = daemon.db.get_capture(uid)
    if row is None:
        return {"error": f"uid {uid!r} not found"}

    full = (
        daemon.lmdb_source.fetch_record(int(row["ob_id"]))
        if daemon.lmdb_source is not None and row.get("ob_id")
        else None
    )
    if full is None:
        return {"error": f"capture {uid} has no LMDB record"}

    sess = full.get("session") or {}
    cap_req = sess.get("request") or {}
    rl = cap_req.get("requestLine") or {}

    actual_method = (method or rl.get("method") or row.get("method") or "GET").upper()
    if not actual_method:
        return {"error": "could not determine request method"}

    actual_url = url or row.get("url") or ""
    if not actual_url:
        # Reconstruct from connection + path.
        conn = sess.get("connection") or {}
        scheme = "https" if conn.get("security") else "http"
        host = conn.get("originHost") or row.get("host") or ""
        if not host:
            return {"error": "could not determine host for replay"}
        # IPv6 literals must be bracketed in URLs (RFC 3986 §3.2.2).
        # We detect by colon presence: hostnames don't contain ':'.
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        path = rl.get("path") or row.get("path") or "/"
        actual_url = f"{scheme}://{host}{path}"
    parsed = urlparse(actual_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"error": f"invalid replay URL: {actual_url!r}"}

    merged_headers = _merge_headers(cap_req.get("headers") or [], headers)

    # Body resolution: explicit override > captured > empty.
    body_bytes: bytes | None
    auto_ct: str | None = None
    if body is None:
        # Use captured body for methods that typically carry one.
        body_bytes = None
        if daemon.body_source is not None and actual_method in (
            "POST", "PUT", "PATCH", "DELETE"
        ):
            lookup = lookup_from_record(full)
            if lookup is not None:
                body_bytes = daemon.body_source.get_request_body(lookup)
        if body_bytes is None:
            body_bytes = b""
    else:
        body_bytes, auto_ct, err = _coerce_body(body)
        if err is not None:
            return {"error": err}
        if body_bytes is None:  # explicit ""? coerce returns (b"",None,None)
            body_bytes = b""

    if auto_ct and not any(
        k.lower() == "content-type" for k, _ in merged_headers
    ):
        merged_headers.append(("Content-Type", auto_ct))

    req = urllib.request.Request(
        actual_url, data=body_bytes if body_bytes else None, method=actual_method
    )
    for name, value in merged_headers:
        req.add_header(name, value)

    # Empty ProxyHandler explicitly — do NOT inherit OS / env proxies.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )

    started = time.monotonic()
    try:
        with opener.open(req, timeout=timeout_seconds) as resp:
            response_bytes = resp.read()
            status = resp.status
            reason = resp.reason or ""
            resp_headers = [f"{k}: {v}" for k, v in resp.getheaders()]
            actual_url_final = resp.url  # follows redirects
    except urllib.error.HTTPError as e:
        # 4xx/5xx still produce a meaningful response — surface it.
        response_bytes = e.read() or b""
        status = e.code
        reason = e.reason or ""
        resp_headers = [f"{k}: {v}" for k, v in e.headers.items()]
        actual_url_final = e.url or actual_url
    except TimeoutError:
        return {"error": f"timeout after {timeout_seconds}s"}
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        return {"error": f"network error: {type(e).__name__}: {e}"}
    rtt_ms = int((time.monotonic() - started) * 1000)

    text, enc = _decode_body(response_bytes)
    return {
        "status": status,
        "status_message": reason,
        "headers": resp_headers,
        "response_body": text,
        "response_body_encoding": enc,
        "response_body_size": len(response_bytes),
        "rtt_ms": rtt_ms,
        "url_actual": actual_url_final,
        "method": actual_method,
    }


__all__: list[str] = []  # registered via @mcp.tool
