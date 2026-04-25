"""Tests for the M17.1 ``replay_request`` MCP tool.

Helpers (pure functions: ``_split_header``, ``_ci_dict``,
``_merge_headers``, ``_coerce_body``) are tested directly. The
high-level ``replay_request`` tool is exercised end-to-end against a
local ``http.server`` listening on a random port — no network access
required, no live Reqable LMDB needed (we mock the daemon). This is
the level at which ``ProxyHandler({})`` and the body merge logic
matter, and only an end-to-end test catches a mistake there.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from unittest.mock import MagicMock

import pytest

from reqable_mcp.mcp_server import set_daemon
from reqable_mcp.rules import BODY_MAX_BYTES
from reqable_mcp.tools.replay import (
    _ci_dict,
    _coerce_body,
    _merge_headers,
    _split_header,
    replay_request,
)

# ---------------------------------------------------------------- helpers


class TestSplitHeader:
    def test_basic(self) -> None:
        assert _split_header("Host: api.example.com") == ("Host", "api.example.com")

    def test_strips_whitespace(self) -> None:
        assert _split_header("  X-Foo  :  bar  ") == ("X-Foo", "bar")

    def test_pseudo_header_dropped(self) -> None:
        assert _split_header(":method: GET") is None

    def test_no_colon_dropped(self) -> None:
        assert _split_header("garbage") is None

    def test_empty_dropped(self) -> None:
        assert _split_header("") is None


class TestCiDict:
    def test_lowercases_keys(self) -> None:
        d = _ci_dict(["Content-Type: application/json", "X-Foo: bar"])
        assert d == {"content-type": "application/json", "x-foo": "bar"}

    def test_skips_pseudo_and_garbage(self) -> None:
        d = _ci_dict([":method: GET", "garbage", "X-Real: 1"])
        assert d == {"x-real": "1"}


class TestMergeHeaders:
    def test_passthrough_no_overrides(self) -> None:
        out = _merge_headers(["Host: a.example.com", "Accept: */*"], None)
        assert out == [("Host", "a.example.com"), ("Accept", "*/*")]

    def test_override_replaces_case_insensitively(self) -> None:
        out = _merge_headers(
            ["Host: a.example.com", "Accept: */*"],
            {"accept": "text/plain"},
        )
        assert dict(out)["Accept"] == "text/plain"

    def test_empty_value_deletes(self) -> None:
        out = _merge_headers(
            ["Host: a.example.com", "Accept: */*"],
            {"Accept": ""},
        )
        keys = [k.lower() for k, _ in out]
        assert "accept" not in keys
        assert "host" in keys

    def test_unknown_key_appended(self) -> None:
        out = _merge_headers(["Host: a"], {"X-New": "1"})
        assert ("X-New", "1") in out

    def test_content_length_always_dropped(self) -> None:
        out = _merge_headers(["Host: a", "Content-Length: 42"], None)
        keys = [k.lower() for k, _ in out]
        assert "content-length" not in keys


class TestCoerceBody:
    def test_none_passes_through(self) -> None:
        assert _coerce_body(None) == (None, None, None)

    def test_empty_string(self) -> None:
        assert _coerce_body("") == (b"", None, None)

    def test_string(self) -> None:
        assert _coerce_body("hello") == (b"hello", None, None)

    def test_dict_adds_json_hint(self) -> None:
        data, ct, err = _coerce_body({"a": 1})
        assert err is None
        assert data == b'{"a": 1}'
        assert ct == "application/json"

    def test_list_rejected(self) -> None:
        _, _, err = _coerce_body([1, 2, 3])
        assert err is not None
        assert "must be str, dict, or None" in err

    def test_oversize_string_rejected(self) -> None:
        _, _, err = _coerce_body("x" * (BODY_MAX_BYTES + 1))
        assert err is not None
        assert "BODY_MAX_BYTES" in err

    def test_oversize_dict_rejected(self) -> None:
        _, _, err = _coerce_body({"k": "x" * BODY_MAX_BYTES})
        assert err is not None


# ---------------------------------------------------------------- HTTP fixture


class _Recorder(BaseHTTPRequestHandler):
    """Captures the request and replies with a fixed body.

    The response includes ``X-Echo-Method`` / ``X-Echo-Path`` so the
    test can assert what the *replay* actually sent without parsing
    headers from the request side.
    """

    captured: dict[str, Any] = {}

    def do_GET(self) -> None:  # noqa: N802 — http.server convention
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).captured = {
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        }
        # Slow-path for timeout test.
        if self.path.startswith("/slow"):
            import time

            time.sleep(2.0)
        self.send_response(200, "OK")
        self.send_header("Content-Type", "text/plain")
        self.send_header("X-Replay-Saw-Method", self.command)
        self.end_headers()
        self.wfile.write(b"replayed")

    def log_message(self, *_args, **_kw) -> None:  # silence
        pass


@pytest.fixture
def http_server() -> Iterator[tuple[str, type[_Recorder]]]:
    """Start a local HTTP server on a random port, return its URL +
    the recorder class so a test can inspect what the replay sent."""
    _Recorder.captured = {}
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", _Recorder
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- daemon mock


@pytest.fixture
def mock_daemon():
    """Hand the MCP tools a fake Daemon with a captured row preloaded.

    We don't need a real LMDB / SQLite for replay tests — replay only
    reads from these and never writes. A ``MagicMock`` configured to
    return a synthetic capture is enough.
    """
    daemon = MagicMock()
    daemon.db.get_capture.return_value = {
        "uid": "fake-uid",
        "ob_id": 1,
        "url": "https://api.original.test/v1/login",
        "host": "api.original.test",
        "path": "/v1/login",
        "method": "POST",
    }
    daemon.lmdb_source.fetch_record.return_value = {
        "session": {
            "id": 1,
            "request": {
                "requestLine": {"method": "POST", "path": "/v1/login"},
                "headers": [
                    "Host: api.original.test",
                    "Content-Type: application/json",
                    "X-Captured: yes",
                ],
            },
            "connection": {
                "originHost": "api.original.test",
                "security": True,
                "timestamp": 123,
                "id": 1,
            },
        }
    }
    daemon.body_source.get_request_body.return_value = b'{"u":"alice"}'
    set_daemon(daemon)
    return daemon


# ---------------------------------------------------------------- end-to-end


def test_replay_with_url_override(
    mock_daemon, http_server: tuple[str, type[_Recorder]]
) -> None:
    base, rec = http_server
    out = replay_request(uid="fake-uid", url=f"{base}/echo")
    assert out["status"] == 200, out
    assert out["response_body"] == "replayed"
    # The replay reached our local server with the captured method
    # and body, and our captured X-Captured header survived.
    assert rec.captured["method"] == "POST"
    assert rec.captured["path"] == "/echo"
    assert rec.captured["headers"]["x-captured"] == "yes"
    assert rec.captured["body"] == b'{"u":"alice"}'


def test_method_override(
    mock_daemon, http_server: tuple[str, type[_Recorder]]
) -> None:
    base, rec = http_server
    replay_request(uid="fake-uid", url=f"{base}/x", method="get", body="")
    assert rec.captured["method"] == "GET"
    assert rec.captured["body"] == b""


def test_headers_override_and_delete(
    mock_daemon, http_server: tuple[str, type[_Recorder]]
) -> None:
    base, rec = http_server
    replay_request(
        uid="fake-uid", url=f"{base}/x",
        headers={"X-Captured": "", "X-New": "1", "Content-Type": "text/plain"},
    )
    assert "x-captured" not in rec.captured["headers"]
    assert rec.captured["headers"]["x-new"] == "1"
    assert rec.captured["headers"]["content-type"] == "text/plain"


def test_dict_body_auto_adds_content_type(
    mock_daemon, http_server: tuple[str, type[_Recorder]]
) -> None:
    base, rec = http_server
    # Drop the captured Content-Type to prove the auto-add fires.
    replay_request(
        uid="fake-uid", url=f"{base}/x",
        body={"replayed": True},
        headers={"Content-Type": ""},  # delete captured content-type
    )
    assert rec.captured["headers"]["content-type"] == "application/json"
    assert rec.captured["body"] == b'{"replayed": true}'


def test_explicit_empty_body_clears(
    mock_daemon, http_server: tuple[str, type[_Recorder]]
) -> None:
    base, rec = http_server
    replay_request(uid="fake-uid", url=f"{base}/x", body="")
    assert rec.captured["body"] == b""


def test_string_body(
    mock_daemon, http_server: tuple[str, type[_Recorder]]
) -> None:
    base, rec = http_server
    replay_request(uid="fake-uid", url=f"{base}/x", body="raw text payload")
    assert rec.captured["body"] == b"raw text payload"


def test_proxyhandler_ignores_env_proxy(
    mock_daemon, http_server: tuple[str, type[_Recorder]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon startup sets NO_PROXY=* but the explicit ProxyHandler({})
    must hold even if a malicious caller restored HTTP_PROXY."""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")  # would refuse-connect
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    base, rec = http_server
    out = replay_request(uid="fake-uid", url=f"{base}/x")
    # If the env proxy were honored, this would fail with connection refused.
    assert out.get("status") == 200, out
    assert rec.captured["method"] == "POST"


def test_unknown_uid(mock_daemon) -> None:
    mock_daemon.db.get_capture.return_value = None
    out = replay_request(uid="missing")
    assert "error" in out
    assert "not found" in out["error"]


def test_invalid_url(mock_daemon) -> None:
    out = replay_request(uid="fake-uid", url="ftp://nope/")
    assert "error" in out
    assert "invalid replay URL" in out["error"]


def test_oversized_body_rejected(mock_daemon) -> None:
    out = replay_request(
        uid="fake-uid", url="http://127.0.0.1/x",
        body="x" * (BODY_MAX_BYTES + 1),
    )
    assert "error" in out
    assert "BODY_MAX_BYTES" in out["error"]


def test_unsupported_body_type_rejected(mock_daemon) -> None:
    out = replay_request(
        uid="fake-uid", url="http://127.0.0.1/x",
        body=[1, 2, 3],  # type: ignore[arg-type]
    )
    assert "error" in out


def test_timeout(
    mock_daemon, http_server: tuple[str, type[_Recorder]]
) -> None:
    base, _ = http_server
    out = replay_request(
        uid="fake-uid", url=f"{base}/slow", timeout_seconds=0.2,
    )
    assert "error" in out
    assert "timeout" in out["error"]


def test_timeout_out_of_range(mock_daemon) -> None:
    out = replay_request(
        uid="fake-uid", url="http://127.0.0.1/x", timeout_seconds=300,
    )
    assert "error" in out
    assert "timeout_seconds" in out["error"]


def test_content_length_override_dropped(
    mock_daemon, http_server: tuple[str, type[_Recorder]]
) -> None:
    """A caller-supplied Content-Length must NOT be honored — urllib
    re-computes it from the actual body bytes; honoring an override
    would just create a body/declared-length mismatch."""
    base, rec = http_server
    replay_request(
        uid="fake-uid", url=f"{base}/x",
        body="hello",
        headers={"Content-Length": "999"},  # garbage override
    )
    # http.server reads Content-Length bytes; if the override leaked
    # through, Content-Length=999 with a 5-byte body would have hung
    # the test (rfile.read(999) blocks waiting for more bytes).
    assert rec.captured["body"] == b"hello"
    assert rec.captured["headers"]["content-length"] == "5"


def test_ipv6_host_bracketed(mock_daemon) -> None:
    """When we synthesize a URL from a captured IPv6 connection, the
    literal must be bracketed or urllib will choke on it."""
    # Force url-synthesis path by clearing the captured url.
    mock_daemon.db.get_capture.return_value = {
        "uid": "fake-uid", "ob_id": 1,
        "url": "",  # forces synthesis
        "host": "::1", "path": "/", "method": "GET",
    }
    mock_daemon.lmdb_source.fetch_record.return_value = {
        "session": {
            "id": 1,
            "request": {
                "requestLine": {"method": "GET", "path": "/"},
                "headers": [],
            },
            "connection": {
                "originHost": "::1", "security": False,
                "timestamp": 1, "id": 1,
            },
        }
    }
    out = replay_request(uid="fake-uid", timeout_seconds=0.5)
    # The synthesized URL must round-trip through urlparse without
    # complaint. We can't easily assert the URL string from the
    # outside, but a "could not determine host" or "invalid replay
    # URL" error would mean we never bracketed it. Real connection
    # fails (no listener on ::1:80), but that's an OSError network
    # error — the URL itself was acceptable.
    assert "error" in out
    assert "invalid replay URL" not in out["error"]
    assert "could not determine host" not in out["error"]


def test_synthesis_with_no_host_rejected(mock_daemon) -> None:
    mock_daemon.db.get_capture.return_value = {
        "uid": "fake-uid", "ob_id": 1, "url": "", "host": "",
        "path": "/", "method": "GET",
    }
    mock_daemon.lmdb_source.fetch_record.return_value = {
        "session": {
            "id": 1,
            "request": {
                "requestLine": {"method": "GET", "path": "/"},
                "headers": [],
            },
            "connection": {"originHost": "", "security": False, "timestamp": 1, "id": 1},
        }
    }
    out = replay_request(uid="fake-uid")
    assert "error" in out
    assert "host" in out["error"]


def test_4xx_response_returned_not_raised(
    mock_daemon, http_server: tuple[str, type[_Recorder]],
) -> None:
    """A 404 from the upstream is a real response — we surface status
    + body, not raise."""
    # Override the recorder with one that returns 404.
    base, rec = http_server
    # Re-shadow do_POST to send 404.
    orig = rec.do_POST

    def _send_404(self) -> None:  # type: ignore[no-untyped-def]
        length = int(self.headers.get("Content-Length", "0") or 0)
        self.rfile.read(length)
        self.send_response(404, "Not Found")
        self.end_headers()
        self.wfile.write(b"missing")

    rec.do_POST = _send_404  # type: ignore[assignment]
    try:
        out = replay_request(uid="fake-uid", url=f"{base}/x")
    finally:
        rec.do_POST = orig  # type: ignore[assignment]
    assert out["status"] == 404
    assert out["response_body"] == "missing"
