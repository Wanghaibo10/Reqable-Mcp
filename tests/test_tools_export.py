"""Tests for the M17.2 export tools — decode_body / prettify / dump_body.

The bulk of the surface is pure-function helpers (codec walking,
format detection, path validation) — those get direct unit tests.
The high-level ``decode_body`` / ``prettify`` / ``dump_body`` tools
are exercised against a mocked daemon so we don't need a live
Reqable LMDB to verify the wiring.
"""

from __future__ import annotations

import gzip
import json
import zlib
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from reqable_mcp.mcp_server import set_daemon
from reqable_mcp.tools.export import (
    _content_encoding_from,
    _content_type_from,
    _decode_one,
    _detect_format,
    _pretty_html,
    _pretty_json,
    _pretty_xml,
    _validate_dump_path,
    _walk_content_encoding,
    decode_body,
    dump_body,
    prettify,
)

# ---------------------------------------------------------------- decode helpers


class TestDecodeOne:
    def test_identity(self) -> None:
        out, err = _decode_one(b"hello", "identity")
        assert err is None
        assert out == b"hello"

    def test_empty_codec_passthrough(self) -> None:
        out, err = _decode_one(b"hello", "")
        assert err is None
        assert out == b"hello"

    def test_gzip(self) -> None:
        compressed = gzip.compress(b"payload")
        out, err = _decode_one(compressed, "gzip")
        assert err is None
        assert out == b"payload"

    def test_x_gzip_alias(self) -> None:
        compressed = gzip.compress(b"alias")
        out, err = _decode_one(compressed, "x-gzip")
        assert err is None
        assert out == b"alias"

    def test_deflate_zlib_wrapped(self) -> None:
        compressed = zlib.compress(b"payload")
        out, err = _decode_one(compressed, "deflate")
        assert err is None
        assert out == b"payload"

    def test_deflate_raw(self) -> None:
        comp = zlib.compressobj(-1, zlib.DEFLATED, -zlib.MAX_WBITS)
        compressed = comp.compress(b"raw") + comp.flush()
        out, err = _decode_one(compressed, "deflate")
        assert err is None
        assert out == b"raw"

    def test_unsupported_codec(self) -> None:
        out, err = _decode_one(b"x", "snappy")
        assert out is None
        assert err is not None
        assert "unsupported" in err

    def test_brotli_when_pkg_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from reqable_mcp.tools import export

        monkeypatch.setattr(export, "_try_import_brotli", lambda: None)
        out, err = export._decode_one(b"\x1b\x03\x00\x00x", "br")
        assert out is None
        assert err is not None
        assert "br codec missing" in err

    def test_zstd_when_pkg_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from reqable_mcp.tools import export

        monkeypatch.setattr(export, "_try_import_zstd", lambda: None)
        out, err = export._decode_one(b"\x28\xb5\x2f\xfd\x00", "zstd")
        assert out is None
        assert err is not None
        assert "zstd codec missing" in err


class TestWalkContentEncoding:
    def test_no_encoding(self) -> None:
        out, chain, err = _walk_content_encoding(b"raw", None)
        assert out == b"raw"
        assert chain == []
        assert err is None

    def test_single_gzip(self) -> None:
        out, chain, err = _walk_content_encoding(gzip.compress(b"hi"), "gzip")
        assert out == b"hi"
        assert chain == ["gzip"]
        assert err is None

    def test_chained_right_to_left(self) -> None:
        # Inner: zlib(deflate). Outer: gzip. Header: "deflate, gzip"
        # so we strip gzip first, then deflate.
        inner = zlib.compress(b"layered")
        outer = gzip.compress(inner)
        out, chain, err = _walk_content_encoding(outer, "deflate, gzip")
        assert out == b"layered"
        assert chain == ["gzip", "deflate"]
        assert err is None

    def test_partial_failure_returns_partial(self) -> None:
        # gzip(broken) — outer codec succeeds, inner is bogus
        out, chain, err = _walk_content_encoding(b"not-actually-gzip", "gzip")
        assert err is not None
        # Nothing was successfully applied.
        assert chain == []


class TestHeaderHelpers:
    def test_content_encoding(self) -> None:
        assert _content_encoding_from(["Host: x", "Content-Encoding: gzip"]) == "gzip"

    def test_case_insensitive(self) -> None:
        assert _content_encoding_from(["content-encoding: BR"]) == "BR"

    def test_missing(self) -> None:
        assert _content_encoding_from(["Host: x"]) is None

    def test_skips_pseudo_headers(self) -> None:
        assert _content_encoding_from([":status: 200", "Content-Encoding: gzip"]) == "gzip"

    def test_content_type_strips_charset(self) -> None:
        assert _content_type_from(["Content-Type: application/json; charset=UTF-8"]) == "application/json"


# ---------------------------------------------------------------- format detection


class TestDetectFormat:
    def test_content_type_json(self) -> None:
        assert _detect_format("application/json", "{}") == "json"

    def test_content_type_xml(self) -> None:
        assert _detect_format("application/xml", "<x/>") == "xml"

    def test_content_type_html(self) -> None:
        assert _detect_format("text/html", "<p>") == "html"

    def test_sniff_json(self) -> None:
        assert _detect_format(None, "  {\"k\": 1}") == "json"

    def test_sniff_html_via_doctype(self) -> None:
        assert _detect_format(None, "<!DOCTYPE html><html><body></body></html>") == "html"

    def test_sniff_xml(self) -> None:
        assert _detect_format(None, "<?xml version='1.0'?><root/>") == "xml"

    def test_unknown_text(self) -> None:
        assert _detect_format(None, "plain old text") == "text"


class TestPrettyFormatters:
    def test_json_indents(self) -> None:
        out, err = _pretty_json('{"a":1,"b":[2,3]}')
        assert err is None
        assert "\n" in out
        assert json.loads(out) == {"a": 1, "b": [2, 3]}

    def test_json_invalid_returns_error(self) -> None:
        out, err = _pretty_json("not json")
        assert err is not None
        assert out == "not json"

    def test_xml_indents(self) -> None:
        out, err = _pretty_xml("<a><b>v</b></a>")
        assert err is None
        assert "\n" in out

    def test_html_inserts_newlines(self) -> None:
        out, _ = _pretty_html("<p><b>x</b></p>")
        assert "\n" in out

    def test_html_decodes_entities(self) -> None:
        out, _ = _pretty_html("<p>&quot;hi&quot;</p>")
        assert "\"hi\"" in out


# ---------------------------------------------------------------- path validation


class TestValidateDumpPath:
    def test_relative_rejected(self) -> None:
        out, err = _validate_dump_path("relative/file.bin")
        assert out is None
        assert err is not None
        assert "absolute" in err

    def test_under_reqable_dir_rejected(self) -> None:
        out, err = _validate_dump_path(
            str(
                Path.home() / "Library" / "Application Support"
                / "com.reqable.macosx" / "exfil.bin"
            )
        )
        assert out is None
        assert err is not None
        assert "Reqable's own data" in err

    def test_absolute_tmp_ok(self, tmp_path: Path) -> None:
        target = tmp_path / "out.bin"
        out, err = _validate_dump_path(str(target))
        assert err is None
        assert out == target.resolve()


# ---------------------------------------------------------------- daemon mock


def _make_mock_daemon(
    raw_response: bytes = b"plain body",
    raw_request: bytes = b"req body",
    response_headers: list[str] | None = None,
    request_headers: list[str] | None = None,
) -> Any:
    daemon = MagicMock()
    daemon.db.get_capture.return_value = {
        "uid": "fake-uid",
        "ob_id": 1,
        "url": "https://api.original.test/v1/x",
        "host": "api.original.test",
        "method": "POST",
    }
    daemon.lmdb_source.fetch_record.return_value = {
        "session": {
            "id": 1,
            "request": {
                "headers": request_headers or [
                    "Host: api.original.test",
                    "Content-Type: application/json",
                ],
            },
            "response": {
                "headers": response_headers or ["Content-Type: text/plain"],
            },
            "connection": {"timestamp": 1, "id": 1, "originHost": "x", "security": True},
        }
    }
    daemon.body_source.get_request_body.return_value = raw_request
    daemon.body_source.get_response_raw.return_value = raw_response
    daemon.body_source.get_response_body.return_value = raw_response
    return daemon


# ---------------------------------------------------------------- decode_body tool


class TestDecodeBodyTool:
    def test_response_plain(self) -> None:
        set_daemon(_make_mock_daemon(raw_response=b"plain"))
        out = decode_body("fake-uid", "response")
        assert out["decoded"] == "plain"
        assert out["encoding_chain"] == []
        assert out["original_size"] == 5
        assert out["decoded_size"] == 5

    def test_response_gzip(self) -> None:
        compressed = gzip.compress(b"hello world")
        set_daemon(
            _make_mock_daemon(
                raw_response=compressed,
                response_headers=[
                    "Content-Type: text/plain",
                    "Content-Encoding: gzip",
                ],
            )
        )
        out = decode_body("fake-uid", "response")
        assert out["decoded"] == "hello world"
        assert out["encoding_chain"] == ["gzip"]
        assert out["original_size"] == len(compressed)
        assert out["decoded_size"] == len("hello world")
        assert out["content_type"] == "text/plain"

    def test_request_side(self) -> None:
        set_daemon(_make_mock_daemon(raw_request=b"req payload"))
        out = decode_body("fake-uid", "request")
        assert out["decoded"] == "req payload"

    def test_unknown_uid(self) -> None:
        d = _make_mock_daemon()
        d.db.get_capture.return_value = None
        set_daemon(d)
        out = decode_body("missing", "response")
        assert "error" in out

    def test_binary_falls_back_to_base64(self) -> None:
        set_daemon(_make_mock_daemon(raw_response=b"\xff\xfe\xfd"))
        out = decode_body("fake-uid", "response")
        assert out["decoded_encoding"] == "base64"


# ---------------------------------------------------------------- prettify tool


class TestPrettifyTool:
    def test_json_auto(self) -> None:
        set_daemon(
            _make_mock_daemon(
                raw_response=b'{"a":1,"b":[2,3]}',
                response_headers=["Content-Type: application/json"],
            )
        )
        out = prettify("fake-uid", "response")
        assert out["format"] == "json"
        assert "\n" in out["pretty"]
        assert json.loads(out["pretty"]) == {"a": 1, "b": [2, 3]}

    def test_explicit_format_overrides(self) -> None:
        set_daemon(
            _make_mock_daemon(
                raw_response=b"<a><b>x</b></a>",
                response_headers=["Content-Type: text/plain"],
            )
        )
        out = prettify("fake-uid", "response", format="xml")
        assert out["format"] == "xml"
        assert "\n" in out["pretty"]

    def test_decodes_gzip_first(self) -> None:
        set_daemon(
            _make_mock_daemon(
                raw_response=gzip.compress(b'{"k":1}'),
                response_headers=[
                    "Content-Type: application/json",
                    "Content-Encoding: gzip",
                ],
            )
        )
        out = prettify("fake-uid", "response")
        assert out["format"] == "json"
        assert "gzip" in out["encoding_chain"]
        assert json.loads(out["pretty"]) == {"k": 1}

    def test_binary_body_rejected(self) -> None:
        set_daemon(_make_mock_daemon(raw_response=b"\xff\xfe"))
        out = prettify("fake-uid", "response")
        assert "error" in out
        assert "binary" in out["error"]


# ---------------------------------------------------------------- dump_body tool


class TestDumpBodyTool:
    def test_writes_decoded_body(self, tmp_path: Path) -> None:
        target = tmp_path / "dump.bin"
        set_daemon(
            _make_mock_daemon(
                raw_response=gzip.compress(b"plain content"),
                response_headers=["Content-Encoding: gzip"],
            )
        )
        out = dump_body("fake-uid", "response", str(target))
        assert out["path"] == str(target.resolve())
        assert out["size"] == len(b"plain content")
        assert target.read_bytes() == b"plain content"

    def test_decoded_false_writes_raw(self, tmp_path: Path) -> None:
        target = tmp_path / "raw.bin"
        compressed = gzip.compress(b"plain")
        set_daemon(
            _make_mock_daemon(
                raw_response=compressed,
                response_headers=["Content-Encoding: gzip"],
            )
        )
        out = dump_body("fake-uid", "response", str(target), decoded=False)
        assert target.read_bytes() == compressed
        assert out["encoding_chain"] == []

    def test_relative_path_rejected(self) -> None:
        set_daemon(_make_mock_daemon())
        out = dump_body("fake-uid", "response", "rel.bin")
        assert "error" in out
        assert "absolute" in out["error"]

    def test_under_reqable_rejected(self) -> None:
        set_daemon(_make_mock_daemon())
        out = dump_body(
            "fake-uid", "response",
            str(
                Path.home() / "Library" / "Application Support"
                / "com.reqable.macosx" / "danger.bin"
            ),
        )
        assert "error" in out
        assert "Reqable's own data" in out["error"]

    def test_makes_parent_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "subdir" / "deep" / "out.bin"
        set_daemon(_make_mock_daemon(raw_response=b"hi"))
        out = dump_body("fake-uid", "response", str(target))
        assert target.read_bytes() == b"hi"
        assert "error" not in out
