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


# ---------------------------------------------------------------- export_har


def _make_mock_daemon_for_har(captures: list[dict[str, Any]]) -> Any:
    """Build a daemon mock with multiple captures preloaded.

    Each entry is ``{uid, ob_id, url, host, method, status, ts,
    rtt_ms, ?req_body_size, ?res_body_size, ?req_mime, ?res_mime,
    ?app_name, ?protocol, ?req_headers, ?res_headers,
    ?req_body, ?res_body}``.
    """
    daemon = MagicMock()

    by_uid = {c["uid"]: c for c in captures}

    def get_capture(uid: str):
        c = by_uid.get(uid)
        if c is None:
            return None
        return {
            "uid": c["uid"],
            "ob_id": c.get("ob_id", 1),
            "url": c.get("url", ""),
            "host": c.get("host", ""),
            "path": c.get("path", "/"),
            "method": c.get("method", "GET"),
            "status": c.get("status"),
            "protocol": c.get("protocol", "HTTP/1.1"),
            "ts": c.get("ts", 1700000000000),
            "rtt_ms": c.get("rtt_ms", 0),
            "req_body_size": c.get("req_body_size", 0),
            "res_body_size": c.get("res_body_size", 0),
            "req_mime": c.get("req_mime"),
            "res_mime": c.get("res_mime"),
            "app_name": c.get("app_name"),
        }

    daemon.db.get_capture.side_effect = get_capture

    def query_recent(*, limit: int = 20, host=None, since_ts_ms=None, **_kw):
        rows = []
        for c in captures:
            if host and c.get("host") != host:
                continue
            if since_ts_ms and c.get("ts", 0) < since_ts_ms:
                continue
            rows.append(get_capture(c["uid"]))
        return rows[:limit]

    daemon.db.query_recent.side_effect = query_recent

    def fetch_record(ob_id: int):
        # Find the capture whose ob_id matches.
        for c in captures:
            if c.get("ob_id", 1) == ob_id:
                return {
                    "session": {
                        "id": 1,
                        "request": {
                            "requestLine": {
                                "method": c.get("method", "GET"),
                                "path": c.get("path", "/"),
                            },
                            "protocol": c.get("protocol", "HTTP/1.1"),
                            "headers": c.get("req_headers", []),
                        },
                        "response": {
                            "headers": c.get("res_headers", []),
                            "message": c.get("status_message"),
                        },
                        "connection": {
                            "originHost": c.get("host", ""),
                            "security": True,
                            "timestamp": c.get("ob_id", 1),
                            "id": 1,
                        },
                    }
                }
        return None

    daemon.lmdb_source.fetch_record.side_effect = fetch_record

    def get_request_body(lookup):
        for c in captures:
            if c.get("ob_id", 1) == lookup.conn_timestamp:
                return c.get("req_body")
        return None

    def get_response_body(lookup, *, prefer_decoded=True):
        for c in captures:
            if c.get("ob_id", 1) == lookup.conn_timestamp:
                return c.get("res_body")
        return None

    daemon.body_source.get_request_body.side_effect = get_request_body
    daemon.body_source.get_response_body.side_effect = get_response_body
    daemon.body_source.get_response_raw.side_effect = get_response_body

    return daemon


class TestExportHar:
    def test_unfiltered_refused(self, tmp_path: Path) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(_make_mock_daemon_for_har([]))
        out = export_har(path=str(tmp_path / "x.har"))
        assert "error" in out
        assert "at least one" in out["error"]

    def test_relative_path_refused(self, tmp_path: Path) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(_make_mock_daemon_for_har([{"uid": "u1"}]))
        out = export_har(path="rel.har", uids=["u1"])
        assert "error" in out
        assert "absolute" in out["error"]

    def test_under_reqable_dir_refused(self) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(_make_mock_daemon_for_har([{"uid": "u1"}]))
        bad = (
            Path.home() / "Library" / "Application Support"
            / "com.reqable.macosx" / "evil.har"
        )
        out = export_har(path=str(bad), uids=["u1"])
        assert "error" in out
        assert "Reqable's own data" in out["error"]

    def test_limit_out_of_range(self, tmp_path: Path) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(_make_mock_daemon_for_har([{"uid": "u1"}]))
        for limit in (0, -1, 100_000):
            out = export_har(
                path=str(tmp_path / "x.har"), uids=["u1"], limit=limit
            )
            assert "error" in out
            assert "limit" in out["error"]

    def test_basic_export_one_capture(self, tmp_path: Path) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(
            _make_mock_daemon_for_har([
                {
                    "uid": "u1", "ob_id": 1,
                    "url": "https://api.example.com/v1/login",
                    "host": "api.example.com", "path": "/v1/login",
                    "method": "POST", "status": 200,
                    "ts": 1700000000000, "rtt_ms": 123,
                    "protocol": "h2",
                    "req_mime": "application/json",
                    "res_mime": "application/json",
                    "req_headers": [
                        "host: api.example.com",
                        "content-type: application/json",
                    ],
                    "res_headers": [
                        "content-type: application/json",
                        "content-length: 7",
                    ],
                    "status_message": "OK",
                    "req_body": b'{"u":"a"}',
                    "res_body": b'{"ok":1}',
                    "req_body_size": 9,
                    "res_body_size": 7,
                }
            ])
        )
        target = tmp_path / "out.har"
        out = export_har(path=str(target), uids=["u1"])
        assert "error" not in out, out
        assert out["entry_count"] == 1
        assert out["skipped_count"] == 0
        har = json.loads(target.read_text())
        assert har["log"]["version"] == "1.2"
        assert har["log"]["creator"]["name"] == "reqable-mcp"
        entry = har["log"]["entries"][0]
        assert entry["request"]["method"] == "POST"
        assert entry["request"]["url"] == "https://api.example.com/v1/login"
        assert entry["request"]["httpVersion"] == "HTTP/2"
        # postData populated for POST with body
        assert entry["request"]["postData"]["mimeType"] == "application/json"
        assert entry["request"]["postData"]["text"] == '{"u":"a"}'
        assert entry["response"]["status"] == 200
        assert entry["response"]["statusText"] == "OK"
        assert entry["response"]["content"]["text"] == '{"ok":1}'
        assert entry["timings"]["wait"] == 123
        assert entry["time"] == 123

    def test_querystring_extracted(self, tmp_path: Path) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(
            _make_mock_daemon_for_har([
                {
                    "uid": "u1", "ob_id": 1,
                    "url": "https://x.test/search?q=hello&page=2",
                    "host": "x.test", "method": "GET", "status": 200,
                }
            ])
        )
        out = export_har(path=str(tmp_path / "qs.har"), uids=["u1"])
        assert "error" not in out
        har = json.loads((tmp_path / "qs.har").read_text())
        qs = har["log"]["entries"][0]["request"]["queryString"]
        assert {"name": "q", "value": "hello"} in qs
        assert {"name": "page", "value": "2"} in qs

    def test_redirect_url_from_location_header(
        self, tmp_path: Path
    ) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(
            _make_mock_daemon_for_har([
                {
                    "uid": "u1", "ob_id": 1,
                    "url": "https://x.test/old",
                    "host": "x.test", "method": "GET",
                    "status": 302,
                    "res_headers": ["Location: https://x.test/new"],
                }
            ])
        )
        export_har(path=str(tmp_path / "r.har"), uids=["u1"])
        har = json.loads((tmp_path / "r.har").read_text())
        assert (
            har["log"]["entries"][0]["response"]["redirectURL"]
            == "https://x.test/new"
        )

    def test_binary_response_body_base64(self, tmp_path: Path) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(
            _make_mock_daemon_for_har([
                {
                    "uid": "u1", "ob_id": 1,
                    "url": "https://x.test/img.png",
                    "host": "x.test", "method": "GET",
                    "status": 200,
                    "res_mime": "image/png",
                    "res_body": b"\x89PNG\r\n\x1a\n\xff\xfe\xfd",
                }
            ])
        )
        export_har(path=str(tmp_path / "bin.har"), uids=["u1"])
        har = json.loads((tmp_path / "bin.har").read_text())
        content = har["log"]["entries"][0]["response"]["content"]
        assert content.get("encoding") == "base64"

    def test_pseudo_headers_dropped(self, tmp_path: Path) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(
            _make_mock_daemon_for_har([
                {
                    "uid": "u1", "ob_id": 1,
                    "url": "https://x.test/",
                    "host": "x.test", "method": "GET",
                    "status": 200, "protocol": "h2",
                    "req_headers": [
                        ":method: GET",
                        ":path: /",
                        "host: x.test",
                        "user-agent: test",
                    ],
                    "res_headers": [":status: 200", "x-real: 1"],
                }
            ])
        )
        export_har(path=str(tmp_path / "h2.har"), uids=["u1"])
        har = json.loads((tmp_path / "h2.har").read_text())
        entry = har["log"]["entries"][0]
        names = [h["name"] for h in entry["request"]["headers"]]
        assert ":method" not in names
        assert ":path" not in names
        assert "host" in names
        res_names = [h["name"] for h in entry["response"]["headers"]]
        assert ":status" not in res_names

    def test_missing_capture_increments_skipped(self, tmp_path: Path) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(
            _make_mock_daemon_for_har([
                {
                    "uid": "u1", "ob_id": 1,
                    "url": "https://x.test/",
                    "host": "x.test", "method": "GET", "status": 200,
                }
            ])
        )
        out = export_har(path=str(tmp_path / "skip.har"), uids=["u1", "missing"])
        assert out["entry_count"] == 1
        assert out["skipped_count"] == 1

    def test_host_filter_uses_query_recent(self, tmp_path: Path) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(
            _make_mock_daemon_for_har([
                {"uid": "u1", "ob_id": 1, "url": "https://a/", "host": "a", "method": "GET", "status": 200},
                {"uid": "u2", "ob_id": 2, "url": "https://b/", "host": "b", "method": "GET", "status": 200},
            ])
        )
        out = export_har(path=str(tmp_path / "host.har"), host="a")
        assert "error" not in out
        har = json.loads((tmp_path / "host.har").read_text())
        urls = [e["request"]["url"] for e in har["log"]["entries"]]
        assert urls == ["https://a/"]

    def test_iso_timestamp_format(self, tmp_path: Path) -> None:
        from reqable_mcp.tools.export import export_har

        set_daemon(
            _make_mock_daemon_for_har([
                {
                    "uid": "u1", "ob_id": 1,
                    "url": "https://x/",
                    "host": "x", "method": "GET", "status": 200,
                    "ts": 1700000000123,  # known epoch ms
                }
            ])
        )
        export_har(path=str(tmp_path / "ts.har"), uids=["u1"])
        har = json.loads((tmp_path / "ts.har").read_text())
        # Format: 2023-11-14T22:13:20.123Z
        ts = har["log"]["entries"][0]["startedDateTime"]
        assert ts.endswith(".123Z")
        assert ts.startswith("2023-")
