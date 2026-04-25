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
import re
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


def _store_relay(name: str, value: str, ttl_seconds: int) -> bool:
    """Send an extracted token to the daemon's RelayStore.

    Returns True iff the daemon confirmed the store. Callers use this
    to decide whether to record a hit — a rejected store (oversized
    value, daemon-side validation error) shouldn't count as the rule
    "applying."
    """
    resp = _ipc_call(
        "store_relay_value",
        {"name": name, "value": value, "ttl_seconds": ttl_seconds},
    )
    return bool(resp and resp.get("ok"))


def _report_dry_run(rule_id: str, context, msg, side: str) -> None:
    """Tell the daemon a dry-run rule matched. Fire-and-forget; an
    IPC failure here doesn't break the request."""
    if not rule_id:
        return
    if side == "request":
        method = getattr(msg, "method", "")
        path = getattr(msg, "path", "")
    else:
        req = getattr(msg, "request", None)
        method = getattr(req, "method", "") if req is not None else ""
        path = getattr(req, "path", "") if req is not None else ""
    _ipc_call(
        "report_dry_run",
        {
            "rule_id": rule_id,
            "uid": context.uid,
            "host": context.host,
            "path": path,
            "method": method,
            "side": side,
        },
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


def _patch_json_body(body, dotted_path: str, value) -> bool:
    """Set ``body`` JSON ``dotted_path`` to ``value``. Returns True on
    success, False if body is not text JSON or the path is unwalkable.

    This *does* mutate ``msg.body``: the rule's purpose is to rewrite
    one field of the in-flight payload. Reqable will re-serialize
    after we return, so the bytes that go upstream / to the client
    will reflect the change. (Contrast with ``relay_extract`` which
    must NOT mutate.)

    Walking semantics:
    * ``""`` is rejected — caller must point at a specific field.
    * Integer-only components index lists; otherwise treat as dict
      keys. ``items.0.price`` works.
    * Intermediate dict keys that don't exist are auto-created.
    * Intermediate list indexes that don't exist fail (we don't
      grow lists implicitly — too easy to corrupt).
    """
    if not dotted_path:
        return False
    try:
        if not getattr(body, "isText", False):
            return False
        raw = body.payload
    except Exception:  # noqa: BLE001
        return False
    if isinstance(raw, dict):
        # Body was already jsonified earlier — work on the dict directly.
        document: object = raw
        was_dict = True
    elif isinstance(raw, str):
        try:
            document = json.loads(raw)
        except (ValueError, TypeError):
            return False
        was_dict = False
    else:
        return False
    if not isinstance(document, (dict, list)):
        return False

    parts = dotted_path.split(".")
    cursor = document
    for i, part in enumerate(parts[:-1]):
        if isinstance(cursor, dict):
            if part not in cursor or not isinstance(cursor[part], (dict, list)):
                # Auto-create missing intermediate dict nodes only when
                # the *next* component looks like a dict key (not int).
                next_part = parts[i + 1]
                cursor[part] = [] if next_part.lstrip("-").isdigit() else {}
            cursor = cursor[part]
        elif isinstance(cursor, list):
            try:
                idx = int(part)
            except ValueError:
                return False
            if not (0 <= idx < len(cursor)):
                return False
            cursor = cursor[idx]
        else:
            return False

    leaf = parts[-1]
    if isinstance(cursor, dict):
        cursor[leaf] = value
    elif isinstance(cursor, list):
        try:
            idx = int(leaf)
        except ValueError:
            return False
        if not (0 <= idx < len(cursor)):
            return False
        cursor[idx] = value
    else:
        return False

    # Write back. If the body was already a dict (some earlier rule
    # jsonified it) leave it as a dict — Reqable serializer handles
    # both. If it was text, replace the text payload.
    if was_dict:
        # Already mutated in place via cursor (it's the same object).
        return True
    body.text(json.dumps(document, ensure_ascii=False))  # type: ignore[attr-defined]
    return True


def _regex_replace_body(body, pattern: str, replacement: str,
                        count: int, flags: int) -> bool:
    """Run ``re.sub`` over ``body``'s text payload.

    ``count=0`` means "replace all" (``re.sub`` semantics). Returns
    True on success, False if body is not text or the pattern fails
    to compile (we already pre-compiled in the daemon, but a corrupt
    persisted rule can still get here).
    """
    try:
        if not getattr(body, "isText", False):
            return False
        raw = body.payload
    except Exception:  # noqa: BLE001
        return False
    if isinstance(raw, dict):
        # Reqable's HttpBody can carry a parsed dict if a previous
        # rule called .jsonify(). Re-serialize, regex over the text.
        try:
            text = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            return False
    elif isinstance(raw, str):
        text = raw
    else:
        return False
    try:
        new_text, n = re.subn(pattern, replacement, text, count=count, flags=flags)
    except re.error:
        return False
    if n == 0:
        # Nothing matched — don't bump hit counter, don't rewrite body.
        return False
    body.text(new_text)  # type: ignore[attr-defined]
    return True


def _extract_from_json_body(body, dotted_path: str) -> str | None:
    """Walk a dotted path through a JSON body. Returns str or None.

    Lists are addressable via integer components: ``data.items.0.token``.

    **Important:** we deliberately do NOT call ``body.jsonify()``.
    That mutator would replace the body's text payload with a parsed
    dict, and Reqable would re-serialize it before sending — quietly
    rewriting the bytes the client receives (whitespace, key order,
    Content-Length mismatch). We parse a private copy instead and
    leave the original untouched.
    """
    if not dotted_path:
        return None
    try:
        if not getattr(body, "isText", False):
            return None
        raw = body.payload
    except Exception:  # noqa: BLE001 - Reqable HttpBody can raise on weird state
        return None
    if isinstance(raw, dict):
        # Some earlier rule may have already jsonify'd this body; tolerate.
        cursor: object = raw
    elif isinstance(raw, str):
        try:
            cursor = json.loads(raw)
        except (ValueError, TypeError):
            return None
    else:
        return None
    if not isinstance(cursor, (dict, list)):
        return None
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

# Deterministic apply order. The daemon may pack rules in any order
# (block-first, then by IPC payload size to fit the frame cap), but
# the addons side needs a stable, semantically-sensible order or
# rules silently shadow each other. Lower number wins.
#
# Request side: relay_inject sets a header *before* a manual
# inject_header could overwrite it; a replace_body rule overwrites
# the inject body last; tag/comment touch only ``context`` so they
# come last.
_REQUEST_KIND_ORDER: dict[str, int] = {
    "relay_inject": 0,
    "inject_header": 1,
    "patch_field": 2,        # surgical JSON patches
    "regex_replace": 3,      # broader text rewrites
    "replace_body": 4,       # whole-body replacement (last word)
    "tag": 9,
    "comment": 9,
}

# Response side: relay_extract MUST run before any rule that mutates
# the body, otherwise we'd extract from a doctored payload.
_RESPONSE_KIND_ORDER: dict[str, int] = {
    "relay_extract": 0,
    "inject_header": 1,
    "patch_field": 2,
    "regex_replace": 3,
    "replace_body": 4,
    "mock": 5,
    "tag": 9,
    "comment": 9,
}


def _sort_key_request(rule: dict) -> int:
    return _REQUEST_KIND_ORDER.get(rule.get("kind"), 9)


def _sort_key_response(rule: dict) -> int:
    return _RESPONSE_KIND_ORDER.get(rule.get("kind"), 9)


def _apply_rule(rule: dict, context, msg, side: str) -> bool:
    """Apply one rule. Returns True if applied (caller bumps hit counter).

    Each kind silently no-ops on shape errors — addons must not raise
    in production paths or it'll abort the request.

    Dry-run short-circuit: if ``rule["dry_run"]`` is truthy we record
    the match (so the operator can see "would have triggered") and
    return True for hit-counting purposes, but skip the actual
    mutation. Block rules are *also* dry-runnable: see
    ``onRequest`` — it consults ``dry_run`` before raising.
    """
    if rule.get("dry_run"):
        rid = rule.get("id")
        if isinstance(rid, str):
            _report_dry_run(rid, context, msg, side)
        return True
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
        elif kind == "patch_field":
            field_path = rule.get("field_path")
            if not isinstance(field_path, str):
                return False
            # ``value`` can legitimately be None / 0 / "" / [] / {} —
            # we only refuse to apply when the key is missing entirely.
            if "value" not in rule:
                return False
            return _patch_json_body(msg.body, field_path, rule["value"])
        elif kind == "regex_replace":
            pattern = rule.get("pattern")
            replacement = rule.get("replacement", "")
            count = rule.get("count", 0)
            flags_int = rule.get("flags", 0)
            if not isinstance(pattern, str) or not isinstance(replacement, str):
                return False
            return _regex_replace_body(
                msg.body, pattern, replacement,
                int(count) if isinstance(count, int) else 0,
                int(flags_int) if isinstance(flags_int, int) else 0,
            )
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
            stored = _store_relay(
                relay_name, extracted,
                int(ttl) if isinstance(ttl, int) else 300,
            )
            # If the daemon rejected the store (oversized value,
            # cardinality cap, etc.) don't count this as a hit — the
            # relay didn't actually take effect.
            return stored
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
    # Separate dry-run block matches from real ones — dry-run blocks
    # must NOT abort, just log and let traffic through.
    real_blocks = [r for r in block_rules if not r.get("dry_run")]
    dry_blocks = [r for r in block_rules if r.get("dry_run")]
    for r in dry_blocks:
        rid = r.get("id")
        if isinstance(rid, str):
            _report_dry_run(rid, context, request, "request")
    if real_blocks:
        block_hits = [
            r["id"] for r in real_blocks if isinstance(r.get("id"), str)
        ]
        # Also credit dry-run blocks with hit counts (they matched).
        for r in dry_blocks:
            rid = r.get("id")
            if isinstance(rid, str):
                block_hits.append(rid)
        _report_hits("request", context, block_hits)
        raise RuntimeError(
            f"reqable-mcp blocked by rule "
            f"{real_blocks[0].get('id', '?')}"
        )
    # No real block: dry-run blocks still count as hits.
    if dry_blocks:
        dry_hits = [
            r["id"] for r in dry_blocks if isinstance(r.get("id"), str)
        ]
        _report_hits("request", context, dry_hits)
    # Stable, semantically-meaningful apply order — see the comment
    # on ``_REQUEST_KIND_ORDER``. Sorted is stable, so equal-priority
    # rules retain the order the daemon sent them (block-first,
    # ascending size). Block rules were fully handled above (real
    # raised + dry-run logged + hits credited); skip them here.
    non_block = [r for r in rules if r.get("kind") != "block"]
    non_block.sort(key=_sort_key_request)
    hits: list[str] = []
    for r in non_block:
        if _apply_rule(r, context, request, "request"):
            rid = r.get("id")
            if isinstance(rid, str):
                hits.append(rid)
    _report_hits("request", context, hits)
    return request


def onResponse(context, response):
    rules = _fetch_rules("response", context, response)
    rules.sort(key=_sort_key_response)
    hits: list[str] = []
    for r in rules:
        if _apply_rule(r, context, response, "response"):
            rid = r.get("id")
            if isinstance(rid, str):
                hits.append(rid)
    _report_hits("response", context, hits)
    return response
