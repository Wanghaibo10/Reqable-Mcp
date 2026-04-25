"""reqable-mcp addons hook.

Runs once per HTTP request/response in the Python interpreter Reqable
forks. We deliberately keep this tiny — heavy logic (rule storage,
hit logging) lives in the long-running ``reqable-mcp serve`` daemon
and we ask it over a Unix socket.

Failure mode is **fail-open**: any error talking to the daemon (no
socket, timeout, malformed reply, daemon down) — we hand the request
back unchanged and let traffic pass. The user's traffic must not break
if our daemon is mid-restart or never installed.

API contract (copied from Reqable's SDK, ``reqable.py``):

* ``onRequest(context, request)``   — return ``request`` or modified.
* ``onResponse(context, response)`` — return ``response`` or modified.

The ``context`` object exposes ``host`` / ``url`` / ``app`` / etc., plus
``highlight`` / ``comment`` / ``env`` setters that Reqable persists
back into LMDB.
"""

# Hook scripts may run under whatever ``script_environment.executor``
# Reqable is configured with — typically the system ``python3``, which
# can be 3.8 on older macOS. Future annotations let us write
# ``dict | None`` etc. without forcing a 3.10+ runtime.
from __future__ import annotations

# ruff: noqa: E402, F403, F405  — Reqable's SDK lives next to us;
# wildcard import gives us Highlight/HttpResponse/HttpHeaders without
# guessing what they're called.
from reqable import *  # type: ignore[import-not-found]

import json
import os
import socket
import sys

# Where the daemon listens. Defaults to the standard install location;
# override via REQABLE_MCP_SOCKET env var (set by install_hook.sh /
# tests when our_data lives somewhere non-default).
SOCKET_PATH = os.environ.get(
    "REQABLE_MCP_SOCKET",
    os.path.expanduser("~/.reqable-mcp/daemon.sock"),
)

# Hard caps so a wedged daemon can never wedge a user's request.
# 300 ms is generous — a healthy round-trip is < 5 ms locally.
SOCKET_TIMEOUT_S = 0.3
PROTOCOL_VERSION = 1


def _eprint(msg: str) -> None:
    """Diagnostic to stderr (Reqable surfaces this in its log panel)."""
    print(f"[reqable-mcp addons] {msg}", file=sys.stderr)


def _ipc_call(op: str, args: dict) -> dict | None:
    """Single-shot IPC round-trip. Returns parsed JSON on success,
    ``None`` on any error (caller should fail-open)."""
    if not os.path.exists(SOCKET_PATH):
        return None
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(SOCKET_TIMEOUT_S)
    try:
        s.connect(SOCKET_PATH)
        payload = json.dumps(
            {"v": PROTOCOL_VERSION, "op": op, "args": args}, ensure_ascii=False
        )
        s.sendall((payload + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf:
            return None
        line = buf.split(b"\n", 1)[0]
        return json.loads(line)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        _eprint(f"ipc {op} failed: {e}")
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass


def _fetch_rules(side: str, context, msg) -> list[dict]:
    """Ask the daemon for rules to apply to this in-flight message."""
    if side == "request":
        method = msg.method
        path = msg.path
    else:
        method = msg.request.method
        path = msg.request.path
    resp = _ipc_call(
        "get_rules",
        {"side": side, "host": context.host, "path": path, "method": method},
    )
    if resp is None or not resp.get("ok"):
        return []
    data = resp.get("data") or []
    return data if isinstance(data, list) else []


def _report_hits(side: str, context, hits: list[str]) -> None:
    """Fire-and-forget — failures are non-fatal."""
    if not hits:
        return
    _ipc_call(
        "report_hit",
        {"side": side, "uid": context.uid, "rule_ids": hits},
    )


def _store_relay(name: str, value: str, ttl_seconds: int) -> None:
    """Send an extracted token to the daemon's RelayStore."""
    _ipc_call(
        "store_relay_value",
        {"name": name, "value": value, "ttl_seconds": ttl_seconds},
    )


def _get_relay(name: str) -> str | None:
    """Fetch a stored relay value. Returns None on miss / IPC failure."""
    resp = _ipc_call("get_relay_value", {"name": name})
    if resp is None or not resp.get("ok"):
        return None
    data = resp.get("data") or {}
    v = data.get("value")
    return v if isinstance(v, str) else None


def _extract_from_header(headers, field: str) -> str | None:
    """Find a header by name (case-insensitive) on a Reqable HttpHeaders."""
    if not isinstance(field, str) or not field:
        return None
    try:
        v = headers[field]
        return v if isinstance(v, str) else None
    except Exception:  # noqa: BLE001 — Reqable headers raise on weird input
        return None


def _extract_from_json_body(body, dotted_path: str) -> str | None:
    """Walk a dotted path through a JSON body. Returns str or None.

    ``msg.body`` is a Reqable HttpBody. We jsonify() to get a dict if
    the body is text, then index. Lists are supported via integer
    components: ``data.items.0.token``.
    """
    if not dotted_path:
        return None
    try:
        body.jsonify()  # noqa: F811 — Reqable HttpBody
    except Exception:  # noqa: BLE001
        return None
    payload = body.payload
    if not isinstance(payload, (dict, list)):
        return None
    cursor = payload
    for part in dotted_path.split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        elif isinstance(cursor, list):
            try:
                idx = int(part)
            except ValueError:
                return None
            if not (0 <= idx < len(cursor)):
                return None
            cursor = cursor[idx]
        else:
            return None
        if cursor is None:
            return None
    return cursor if isinstance(cursor, str) else None


# Highlight enum mapping — Reqable's ``Highlight`` is an IntEnum
# (none/red/yellow/green/blue/teal/strikethrough). We accept either
# the lower-case name or the int directly.
_HIGHLIGHTS = {
    "none": 0, "red": 1, "yellow": 2, "green": 3,
    "blue": 4, "teal": 5, "strikethrough": 6,
}


def _apply_rule(rule: dict, context, msg, side: str) -> bool:
    """Apply one rule. Returns True if applied (caller bumps hit counter).

    Each kind silently no-ops on shape errors — addons must not raise
    in production paths or it'll abort the request.
    """
    kind = rule.get("kind")
    try:
        if kind == "tag":
            color = rule.get("color")
            if isinstance(color, str) and color in _HIGHLIGHTS:
                context.highlight = Highlight(_HIGHLIGHTS[color])  # noqa: F405
                return True
        elif kind == "comment":
            text = rule.get("text")
            if isinstance(text, str):
                context.comment = text
                return True
        elif kind == "inject_header":
            name = rule.get("name")
            value = rule.get("value")
            if isinstance(name, str) and isinstance(value, str):
                msg.headers[name] = value
                return True
        elif kind == "replace_body":
            body = rule.get("body")
            # bytes never reach here — IPC is JSON. str/dict only.
            if isinstance(body, (str, dict)):
                msg.body = body
                return True
        elif kind == "mock" and side == "response":
            status = rule.get("status")
            if isinstance(status, int):
                msg.code = status
            for h, v in (rule.get("headers") or {}).items():
                if isinstance(h, str) and isinstance(v, str):
                    msg.headers[h] = v
            if "body" in rule and isinstance(rule["body"], (str, dict)):
                msg.body = rule["body"]
            return True
        elif kind == "relay_extract" and side == "response":
            relay_name = rule.get("name")
            source_loc = rule.get("source_loc")
            source_field = rule.get("source_field")
            ttl = rule.get("ttl_seconds", 300)
            if not (isinstance(relay_name, str) and isinstance(source_field, str)):
                return False
            extracted: str | None = None
            if source_loc == "header":
                # Pull from response headers (the response we just got).
                extracted = _extract_from_header(msg.headers, source_field)
            elif source_loc == "json_body":
                extracted = _extract_from_json_body(msg.body, source_field)
            if extracted is None:
                return False
            _store_relay(
                relay_name, extracted,
                int(ttl) if isinstance(ttl, int) else 300,
            )
            return True
        elif kind == "relay_inject" and side == "request":
            relay_name = rule.get("name")
            target_header = rule.get("target_header")
            value_prefix = rule.get("value_prefix", "")
            if not (isinstance(relay_name, str) and isinstance(target_header, str)):
                return False
            value = _get_relay(relay_name)
            if value is None:
                return False
            if isinstance(value_prefix, str) and value_prefix:
                value = value_prefix + value
            msg.headers[target_header] = value
            return True
        # ``block`` is handled by the caller (see onRequest) — it needs
        # the hit reported *before* the request is aborted.
    except Exception as e:  # noqa: BLE001
        _eprint(f"rule {rule.get('id')} application failed: {e}")
        return False
    return False


def onRequest(context, request):
    rules = _fetch_rules("request", context, request)
    # Two-pass: any block rule wins outright. We do NOT apply
    # inject_header / replace_body / etc. on a request that's about
    # to be aborted — those edits would never reach upstream and
    # would just inflate hit counts of rules that had no effect.
    block_rules = [r for r in rules if r.get("kind") == "block"]
    if block_rules:
        block_hits = [
            r["id"] for r in block_rules if isinstance(r.get("id"), str)
        ]
        _report_hits("request", context, block_hits)
        raise RuntimeError(
            f"reqable-mcp blocked by rule "
            f"{block_rules[0].get('id', '?')}"
        )
    hits: list[str] = []
    for r in rules:
        if _apply_rule(r, context, request, "request"):
            rid = r.get("id")
            if isinstance(rid, str):
                hits.append(rid)
    _report_hits("request", context, hits)
    return request


def onResponse(context, response):
    rules = _fetch_rules("response", context, response)
    hits: list[str] = []
    for r in rules:
        if _apply_rule(r, context, response, "response"):
            rid = r.get("id")
            if isinstance(rid, str):
                hits.append(rid)
    _report_hits("response", context, hits)
    return response
