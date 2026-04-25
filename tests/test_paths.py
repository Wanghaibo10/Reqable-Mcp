"""Tests for paths.resolve and Paths helpers."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from reqable_mcp.paths import DEFAULT_OUR_DATA, DEFAULT_REQABLE_SUPPORT, resolve


def test_resolve_defaults() -> None:
    p = resolve()
    assert p.reqable_support == DEFAULT_REQABLE_SUPPORT.expanduser().resolve()
    assert p.our_data == DEFAULT_OUR_DATA.expanduser().resolve()
    assert p.reqable_lmdb_dir.name == "box"
    assert p.reqable_capture_dir.name == "capture"
    assert p.reqable_capture_config.name == "capture_config"
    assert p.our_cache_db.name == "cache.db"
    assert p.our_socket.name == "daemon.sock"


def test_resolve_with_overrides(tmp_path: Path) -> None:
    rs = tmp_path / "reqable"
    od = tmp_path / "our"
    p = resolve(reqable_support=rs, our_data=od)
    assert p.reqable_support == rs.resolve()
    assert p.our_data == od.resolve()
    assert p.our_cache_db == (od / "cache.db").resolve()


def test_assert_reqable_present_missing(tmp_path: Path) -> None:
    p = resolve(reqable_support=tmp_path / "nope", our_data=tmp_path / "us")
    with pytest.raises(FileNotFoundError, match="Reqable support dir not found"):
        p.assert_reqable_present()


def test_assert_reqable_present_no_lmdb(tmp_path: Path) -> None:
    rs = tmp_path / "reqable"
    rs.mkdir()
    p = resolve(reqable_support=rs, our_data=tmp_path / "us")
    with pytest.raises(FileNotFoundError, match="LMDB not found"):
        p.assert_reqable_present()


def test_assert_reqable_present_ok(tmp_path: Path) -> None:
    rs = tmp_path / "reqable"
    (rs / "box").mkdir(parents=True)
    (rs / "box" / "data.mdb").write_bytes(b"")
    p = resolve(reqable_support=rs, our_data=tmp_path / "us")
    p.assert_reqable_present()  # no raise


def test_ensure_our_dirs_creates_with_0700(tmp_path: Path) -> None:
    od = tmp_path / "fresh"
    p = resolve(reqable_support=tmp_path, our_data=od)
    p.ensure_our_dirs()

    assert od.exists()
    mode = stat.S_IMODE(os.stat(od).st_mode)
    assert mode == 0o700, f"expected 0o700 perms, got {mode:o}"


def test_ensure_our_dirs_tightens_existing_loose_perms(tmp_path: Path) -> None:
    od = tmp_path / "loose"
    od.mkdir(mode=0o755)
    p = resolve(reqable_support=tmp_path, our_data=od)
    p.ensure_our_dirs()

    mode = stat.S_IMODE(os.stat(od).st_mode)
    assert mode == 0o700
