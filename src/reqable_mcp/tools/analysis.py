"""Tier-5 reverse-engineering helpers.

Tools targeted at the user's web-reverse / scraper workflow:

* ``find_dynamic_fields`` — heuristic detector for fields that change
  request-to-request (likely tokens / signatures / nonces). The
  classic anti-bot identifiers (``sensor_data``, ``_abck``,
  ``__token``, ``X-Bm-Sensor-Data``) fall out of this naturally.
* ``decode_jwt`` — quick header/payload decode for JWTs spotted in
  Authorization or Cookie headers.
* ``extract_auth`` — list every authentication-bearing field across
  recent captures for a host.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qsl, urlparse

from ..db import window_start_ms
from ..mcp_server import get_daemon, mcp

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- helpers


_AUTH_HEADER_PATTERNS = (
    re.compile(r"^authorization$", re.I),
    re.compile(r"^cookie$", re.I),
    re.compile(r"^set-cookie$", re.I),
    re.compile(r"^x-.*-(?:token|auth|key|signature|csrf|sign)$", re.I),
    re.compile(r"^x-csrf-?token$", re.I),
    re.compile(r"^proxy-authorization$", re.I),
    re.compile(r"^api-?key$", re.I),
)

_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_.\-]+\b")


def _split_header(line: str) -> tuple[str, str]:
    name, _, val = line.partition(":")
    return name.strip(), val.strip()


def _is_auth_header(name: str) -> bool:
    return any(p.match(name) for p in _AUTH_HEADER_PATTERNS)


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


# ---------------------------------------------------------------- find_dynamic_fields


@mcp.tool()
def find_dynamic_fields(
    host: str,
    sample_size: int = 20,
    field_locations: list[str] | None = None,
) -> dict[str, Any]:
    """Scan recent captures for fields that vary request-to-request.

    Likely uses:
      * Spotting anti-bot tokens (``sensor_data``, ``_abck``, ``__bm_*``)
      * Locating per-request signatures or nonces
      * Confirming which IDs are session-stable vs request-fresh

    ``field_locations`` may include any subset of:
      * ``"headers"`` (request headers, name+value)
      * ``"queries"`` (URL query parameters)
      * ``"body"``    (top-level keys of JSON request bodies)

    Returns a dict ``{dynamic, stable, occurrences, sample_count, host}``
    where ``dynamic`` is the field names that vary in nearly every
    sampled request, and ``stable`` is the ones that don't.
    """
    if field_locations is None:
        field_locations = ["headers", "queries", "body"]
    daemon = get_daemon()
    if daemon.db is None or daemon.lmdb_source is None:
        return {"dynamic": [], "stable": [], "occurrences": {}, "sample_count": 0, "host": host}

    rows = daemon.db.query_recent(host=host, limit=sample_size)
    if not rows:
        return {
            "dynamic": [],
            "stable": [],
            "occurrences": {},
            "sample_count": 0,
            "host": host,
        }

    # field_name → set of distinct values observed
    seen: dict[str, set[str]] = defaultdict(set)
    occurrences: dict[str, int] = defaultdict(int)

    for row in rows:
        full = daemon.lmdb_source.fetch_record(int(row["ob_id"]))
        if full is None:
            continue
        sess = full.get("session") or {}
        req = sess.get("request") or {}

        if "headers" in field_locations:
            for h in req.get("headers") or []:
                k, v = _split_header(h)
                if not k or k.startswith(":"):
                    continue
                key = f"header:{k.lower()}"
                seen[key].add(v[:200])
                occurrences[key] += 1

        if "queries" in field_locations:
            url = row.get("url") or ""
            try:
                qs = urlparse(url).query
                for k, v in parse_qsl(qs, keep_blank_values=True):
                    key = f"query:{k}"
                    seen[key].add(v[:200])
                    occurrences[key] += 1
            except ValueError:
                pass

        if "body" in field_locations:
            # Reqable sometimes inlines small JSON request bodies in dbData.
            # Robustly try JSON parse; ignore otherwise.
            body = req.get("body") or {}
            payload = body.get("payload") if isinstance(body, dict) else None
            text = None
            if isinstance(payload, dict):
                text = payload.get("text")
            if isinstance(text, str):
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    obj = None
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        key = f"body.{k}"
                        seen[key].add(repr(v)[:200])
                        occurrences[key] += 1

    # Field is "dynamic" if it appears in ≥ 80% of sampled requests AND
    # has > 1 distinct value (i.e. actually varies).
    threshold = max(2, int(0.8 * len(rows)))
    dynamic: list[str] = []
    stable: list[str] = []
    for k, vals in seen.items():
        if occurrences[k] < threshold:
            continue
        if len(vals) >= 2:
            dynamic.append(k)
        else:
            stable.append(k)

    dynamic.sort()
    stable.sort()
    return {
        "host": host,
        "sample_count": len(rows),
        "dynamic": dynamic,
        "stable": stable,
        "occurrences": dict(sorted(occurrences.items())),
    }


# ---------------------------------------------------------------- decode_jwt


@mcp.tool()
def decode_jwt(token_or_uid: str) -> dict[str, Any]:
    """Decode a JWT, or pull the JWT out of a captured request first.

    Argument can be:
      * A raw JWT string (three base64url-encoded parts joined by dots).
      * A capture uid; we then scan its Authorization / Cookie / set-cookie
        headers and return the first JWT found.

    Returns ``{header, payload, signature_b64, source}`` or
    ``{error: ...}`` on failure.
    """
    daemon = get_daemon()

    token: str | None = None
    source = "argument"

    if "." in token_or_uid and token_or_uid.count(".") == 2:
        # Looks like a JWT directly.
        token = token_or_uid
    else:
        # Treat as uid; hunt for a JWT in headers.
        if daemon.db is None or daemon.lmdb_source is None:
            return {"error": "daemon not started"}
        row = daemon.db.get_capture(token_or_uid)
        if row is None:
            return {"error": f"capture {token_or_uid} not found"}
        full = daemon.lmdb_source.fetch_record(int(row["ob_id"]))
        if full is None:
            return {"error": "could not load full record"}
        sess = full.get("session") or {}
        for which in ("request", "response"):
            r = sess.get(which) or {}
            for h in r.get("headers") or []:
                _, val = _split_header(h)
                m = _JWT_RE.search(val)
                if m:
                    token = m.group()
                    source = f"{which}.headers"
                    break
            if token:
                break
        if not token:
            return {"error": "no JWT found in headers"}

    parts = token.split(".")
    if len(parts) != 3:
        return {"error": "not a JWT (expected 3 segments)"}
    try:
        header_json = json.loads(_b64url_decode(parts[0]))
        payload_json = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as e:
        return {"error": f"decode failed: {e}", "source": source}

    return {
        "source": source,
        "header": header_json,
        "payload": payload_json,
        "signature_b64": parts[2],
    }


# ---------------------------------------------------------------- extract_auth


@mcp.tool()
def extract_auth(host: str, window_minutes: int = 60) -> list[dict[str, Any]]:
    """List authentication-bearing fields seen on a host recently.

    Walks the most recent captures in the window and surfaces values
    of headers like ``Authorization``, ``Cookie``, ``X-*-Token``, etc.

    Returns a list, one entry per ``(uid, header_name, header_value)``.
    Cookies are split into individual ``name=value`` pairs for easy
    grep'ing.
    """
    daemon = get_daemon()
    if daemon.db is None or daemon.lmdb_source is None:
        return []

    since = window_start_ms(window_minutes)
    rows = daemon.db.query_recent(host=host, limit=500, since_ts_ms=since)
    out: list[dict[str, Any]] = []
    for row in rows:
        full = daemon.lmdb_source.fetch_record(int(row["ob_id"]))
        if full is None:
            continue
        sess = full.get("session") or {}
        for which in ("request", "response"):
            r = sess.get(which) or {}
            for h in r.get("headers") or []:
                name, val = _split_header(h)
                if not _is_auth_header(name):
                    continue
                if name.lower() == "cookie":
                    # Split the cookie jar.
                    for piece in val.split(";"):
                        piece = piece.strip()
                        if not piece:
                            continue
                        ck_name, _, ck_val = piece.partition("=")
                        out.append(
                            {
                                "uid": row["uid"],
                                "ts": row["ts"],
                                "side": which,
                                "header": "Cookie",
                                "name": ck_name.strip(),
                                "value": ck_val.strip(),
                            }
                        )
                elif name.lower() == "set-cookie":
                    # Each Set-Cookie is one cookie; split off name=value
                    # before the first ';' (attributes follow).
                    head = val.split(";", 1)[0].strip()
                    ck_name, _, ck_val = head.partition("=")
                    out.append(
                        {
                            "uid": row["uid"],
                            "ts": row["ts"],
                            "side": which,
                            "header": "Set-Cookie",
                            "name": ck_name.strip(),
                            "value": ck_val.strip(),
                        }
                    )
                else:
                    out.append(
                        {
                            "uid": row["uid"],
                            "ts": row["ts"],
                            "side": which,
                            "header": name,
                            "value": val,
                        }
                    )
    return out


__all__: list[str] = []
