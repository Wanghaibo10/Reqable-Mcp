"""Tests for the IPC wire format."""

from __future__ import annotations

import json

import pytest

from reqable_mcp.ipc.protocol import (
    LINE_TERMINATOR,
    MAX_MESSAGE_BYTES,
    PROTOCOL_VERSION,
    InvalidMessage,
    decode_message,
    encode_message,
    error_response,
    ok_response,
)


class TestEncode:
    def test_round_trip_basic(self) -> None:
        line = encode_message({"v": 1, "op": "get_rules", "args": {"host": "x"}})
        assert line.endswith(LINE_TERMINATOR)
        obj = json.loads(line[: -len(LINE_TERMINATOR)])
        assert obj["op"] == "get_rules"

    def test_unicode_ok(self) -> None:
        line = encode_message({"v": 1, "op": "x", "args": {"comment": "你好"}})
        assert "你好" in line.decode("utf-8")

    def test_oversized_rejected(self) -> None:
        big = "x" * (MAX_MESSAGE_BYTES + 1)
        with pytest.raises(InvalidMessage, match="exceeds"):
            encode_message({"v": 1, "op": "x", "args": {"big": big}})


class TestDecode:
    def _frame(self, **kw) -> bytes:
        return encode_message({"v": PROTOCOL_VERSION, **kw})

    def test_basic_request(self) -> None:
        req = decode_message(self._frame(op="get_rules", args={"host": "a.com"}))
        assert req.op == "get_rules"
        assert req.args == {"host": "a.com"}

    def test_args_optional(self) -> None:
        req = decode_message(self._frame(op="ping"))
        assert req.args == {}

    def test_strips_trailing_newline(self) -> None:
        line = b'{"v":1,"op":"x","args":{}}\n'
        assert decode_message(line).op == "x"

    def test_handles_missing_newline(self) -> None:
        line = b'{"v":1,"op":"x","args":{}}'
        assert decode_message(line).op == "x"

    def test_rejects_empty(self) -> None:
        with pytest.raises(InvalidMessage, match="empty"):
            decode_message(b"")
        with pytest.raises(InvalidMessage, match="empty"):
            decode_message(b"\n")

    def test_rejects_non_json(self) -> None:
        with pytest.raises(InvalidMessage, match="not JSON"):
            decode_message(b"not json at all\n")

    def test_rejects_non_object(self) -> None:
        with pytest.raises(InvalidMessage, match="not a JSON object"):
            decode_message(b"[1, 2, 3]\n")

    def test_rejects_wrong_version(self) -> None:
        with pytest.raises(InvalidMessage, match="protocol version"):
            decode_message(b'{"v":99,"op":"x","args":{}}\n')

    def test_rejects_missing_op(self) -> None:
        with pytest.raises(InvalidMessage, match="'op'"):
            decode_message(b'{"v":1,"args":{}}\n')

    def test_rejects_empty_op(self) -> None:
        with pytest.raises(InvalidMessage, match="'op'"):
            decode_message(b'{"v":1,"op":"","args":{}}\n')

    def test_rejects_non_object_args(self) -> None:
        with pytest.raises(InvalidMessage, match="'args' must be"):
            decode_message(b'{"v":1,"op":"x","args":"nope"}\n')

    def test_rejects_oversized_input(self) -> None:
        big = b'{"v":1,"op":"x","args":{"big":"' + (b"a" * MAX_MESSAGE_BYTES) + b'"}}\n'
        with pytest.raises(InvalidMessage, match="exceeds"):
            decode_message(big)


class TestResponses:
    def test_ok_response(self) -> None:
        line = ok_response([{"id": "r1"}])
        obj = json.loads(line)
        assert obj == {"ok": True, "data": [{"id": "r1"}]}

    def test_ok_response_no_data(self) -> None:
        line = ok_response()
        obj = json.loads(line)
        assert obj == {"ok": True, "data": {}}

    def test_error_response(self) -> None:
        line = error_response("rate-limited")
        obj = json.loads(line)
        assert obj == {"ok": False, "error": "rate-limited"}

    def test_error_truncated(self) -> None:
        line = error_response("x" * 1000)
        obj = json.loads(line)
        assert len(obj["error"]) == 500
