"""Tests for body_source: capture/ directory body-file reader."""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from reqable_mcp.sources.body_source import (
    BodyLookup,
    BodySource,
    lookup_from_record,
)


@pytest.fixture
def cap_dir(tmp_path: Path) -> Path:
    d = tmp_path / "capture"
    d.mkdir()
    return d


def _write(p: Path, data: bytes) -> None:
    p.write_bytes(data)


def test_filename_request_uses_underscore() -> None:
    lk = BodyLookup(123, 45, 6)
    assert lk.filename("req") == "123-45-6-req_raw-body.reqable"


def test_filename_response_extract_default() -> None:
    lk = BodyLookup(123, 45, 6)
    assert lk.filename("res") == "123-45-6-res-extract-body.reqable"


def test_filename_response_raw_fallback() -> None:
    lk = BodyLookup(123, 45, 6)
    assert (
        lk.filename("res", prefer_decoded=False)
        == "123-45-6-res-raw-body.reqable"
    )


def test_get_request_body_present(cap_dir: Path) -> None:
    src = BodySource(cap_dir)
    lk = BodyLookup(100, 1, 2)
    _write(cap_dir / lk.filename("req"), b"hello world")
    assert src.get_request_body(lk) == b"hello world"


def test_get_request_body_missing(cap_dir: Path) -> None:
    src = BodySource(cap_dir)
    lk = BodyLookup(100, 1, 2)
    assert src.get_request_body(lk) is None


def test_get_response_prefers_extract(cap_dir: Path) -> None:
    """When extract exists, we return that (decoded plaintext)."""
    src = BodySource(cap_dir)
    lk = BodyLookup(100, 1, 2)
    _write(cap_dir / "100-1-2-res-extract-body.reqable", b"<html>plaintext</html>")
    _write(cap_dir / "100-1-2-res-raw-body.reqable", gzip.compress(b"different"))
    assert src.get_response_body(lk) == b"<html>plaintext</html>"


def test_get_response_falls_back_to_raw(cap_dir: Path) -> None:
    """No extract → raw is used. If raw is gzip, it's transparently decoded."""
    src = BodySource(cap_dir)
    lk = BodyLookup(100, 1, 2)
    plaintext = b"some response body"
    _write(cap_dir / "100-1-2-res-raw-body.reqable", gzip.compress(plaintext))
    assert src.get_response_body(lk) == plaintext


def test_get_response_raw_no_decompress(cap_dir: Path) -> None:
    """get_response_raw always returns bytes verbatim — even gzipped."""
    src = BodySource(cap_dir)
    lk = BodyLookup(100, 1, 2)
    raw = gzip.compress(b"hi")
    _write(cap_dir / "100-1-2-res-raw-body.reqable", raw)
    assert src.get_response_raw(lk) == raw


def test_get_response_body_missing(cap_dir: Path) -> None:
    src = BodySource(cap_dir)
    lk = BodyLookup(100, 1, 2)
    assert src.get_response_body(lk) is None


def test_get_response_raw_handles_non_gzip(cap_dir: Path) -> None:
    """If raw doesn't start with the gzip magic, return it as-is."""
    src = BodySource(cap_dir)
    lk = BodyLookup(100, 1, 2)
    _write(cap_dir / "100-1-2-res-raw-body.reqable", b"GIF89a...")
    # No extract and not gzip → returned verbatim
    assert src.get_response_body(lk) == b"GIF89a..."


# --------------------------------------------------------------- lookup_from_record


def test_lookup_from_record_full() -> None:
    rec = {
        "session": {
            "id": 7,
            "connection": {"timestamp": 1777096765092284, "id": 84},
        }
    }
    lk = lookup_from_record(rec)
    assert lk == BodyLookup(1777096765092284, 84, 7)


def test_lookup_from_record_missing_returns_none() -> None:
    assert lookup_from_record({}) is None
    assert lookup_from_record({"session": {}}) is None
    assert lookup_from_record({"session": {"id": 1}}) is None
    assert (
        lookup_from_record({"session": {"id": 1, "connection": {"id": 1}}})
        is None
    )


def test_lookup_from_record_handles_string_values() -> None:
    """Reqable sometimes serializes ports as strings; ints come through fine."""
    rec = {
        "session": {
            "id": "7",  # string
            "connection": {"timestamp": "1777096765092284", "id": "84"},
        }
    }
    lk = lookup_from_record(rec)
    assert lk == BodyLookup(1777096765092284, 84, 7)


def test_lookup_from_record_invalid_types() -> None:
    rec = {
        "session": {
            "id": "not-a-number",
            "connection": {"timestamp": 1, "id": 2},
        }
    }
    assert lookup_from_record(rec) is None
