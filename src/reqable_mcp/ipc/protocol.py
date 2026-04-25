"""Wire format for daemon ↔ addons.py over a Unix socket.

Single-shot, line-delimited JSON. The Reqable-spawned addons process
opens a socket, writes one request line, reads one response line, and
closes. Long connections aren't useful here — addons is short-lived.

Request shape::

    {"v": 1, "op": "<verb>", "args": {...}}

Response shape::

    {"ok": true,  "data": <any>}
    {"ok": false, "error": "<msg>"}

Verbs (initial set; M11.3 + M14/15 may add more):

* ``get_rules``  — args: ``{side, host, path, method}``
                   data: list of rule dicts to apply
* ``report_hit`` — args: ``{side, uid, rule_ids}``
                   data: ``{}``  (fire-and-forget)

The bound is ``MAX_MESSAGE_BYTES``; anything larger is rejected before
``json.loads`` is called. Keeps the daemon insulated from a runaway
addons process pumping garbage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION: int = 1

# 256 KB — generous for rule-list responses (worst case a few hundred
# rules with patterns / payloads), but small enough that a malformed
# stream can't blow out the daemon's memory.
MAX_MESSAGE_BYTES: int = 256 * 1024

LINE_TERMINATOR: bytes = b"\n"


class InvalidMessage(ValueError):  # noqa: N818 — kept for clarity; inherits ValueError
    """Raised when a peer sends a frame we won't process."""


@dataclass(frozen=True)
class Request:
    """One decoded request from addons.py."""

    op: str
    args: dict[str, Any]


def encode_message(payload: dict[str, Any]) -> bytes:
    """Serialize one message frame.

    Adds the line terminator. Caller should write the result with
    ``sock.sendall``.
    """
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(line) > MAX_MESSAGE_BYTES:
        raise InvalidMessage(
            f"outgoing message exceeds {MAX_MESSAGE_BYTES} bytes ({len(line)})"
        )
    return line.encode("utf-8") + LINE_TERMINATOR


def decode_message(line: bytes) -> Request:
    """Parse one inbound request frame.

    Strips the trailing newline if present. Raises
    :class:`InvalidMessage` for any structural problem; never lets a
    malformed frame leak through.
    """
    if len(line) > MAX_MESSAGE_BYTES:
        raise InvalidMessage(f"frame exceeds {MAX_MESSAGE_BYTES} bytes")
    if line.endswith(LINE_TERMINATOR):
        line = line[: -len(LINE_TERMINATOR)]
    if not line:
        raise InvalidMessage("empty frame")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as e:
        raise InvalidMessage(f"not JSON: {e}") from e
    if not isinstance(obj, dict):
        raise InvalidMessage(f"frame is not a JSON object: {type(obj).__name__}")
    v = obj.get("v")
    if v != PROTOCOL_VERSION:
        raise InvalidMessage(
            f"unsupported protocol version {v!r} (expected {PROTOCOL_VERSION})"
        )
    op = obj.get("op")
    if not isinstance(op, str) or not op:
        raise InvalidMessage("missing or invalid 'op'")
    args = obj.get("args") or {}
    if not isinstance(args, dict):
        raise InvalidMessage(f"'args' must be an object, got {type(args).__name__}")
    return Request(op=op, args=args)


def ok_response(data: Any = None) -> bytes:
    """Build a success response frame."""
    return encode_message({"ok": True, "data": data if data is not None else {}})


def error_response(msg: str) -> bytes:
    """Build an error response frame.

    Ends up in addons logs on the Reqable side; keep msg short.
    """
    return encode_message({"ok": False, "error": str(msg)[:500]})


__all__ = [
    "LINE_TERMINATOR",
    "MAX_MESSAGE_BYTES",
    "PROTOCOL_VERSION",
    "InvalidMessage",
    "Request",
    "decode_message",
    "encode_message",
    "error_response",
    "ok_response",
]
