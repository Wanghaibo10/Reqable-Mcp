"""Shared pytest fixtures."""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REQABLE_LMDB_DEFAULT = (
    Path.home() / "Library" / "Application Support" / "com.reqable.macosx" / "box"
)


@pytest.fixture(scope="session")
def reqable_lmdb_path() -> Path | None:
    """Path to the user's real Reqable LMDB, if present.

    Tests that need it use ``@pytest.mark.skipif(not reqable_lmdb_path, ...)``
    via the wrapper fixture below. CI / clean machines simply skip.

    Override by setting ``REQABLE_LMDB_PATH`` env var (handy for fixtures
    pointing at a copied / sanitized LMDB under tests/).
    """
    override = os.environ.get("REQABLE_LMDB_PATH")
    if override:
        p = Path(override)
        return p if (p / "data.mdb").exists() else None
    if (REQABLE_LMDB_DEFAULT / "data.mdb").exists():
        return REQABLE_LMDB_DEFAULT
    return None


@pytest.fixture
def real_lmdb_required(reqable_lmdb_path: Path | None) -> Path:
    """Skip the test if no real Reqable LMDB is reachable."""
    if reqable_lmdb_path is None:
        pytest.skip(
            "real Reqable LMDB unavailable; set REQABLE_LMDB_PATH or run with "
            "Reqable installed and used at least once"
        )
    return reqable_lmdb_path


@pytest.fixture
def short_data_dir() -> Iterator[Path]:
    """A short-path tmp dir for places that need ``AF_UNIX``-safe paths.

    macOS caps Unix-socket paths at 104 bytes; pytest's default
    ``tmp_path`` (under ``/private/var/folders/...``) is too long once
    you append a daemon ``our_data/daemon.sock``. Tests that start the
    Daemon (which now binds an IPC socket by default) should use this
    instead of ``tmp_path``.
    """
    p = Path("/tmp") / f"rmcp-test-{uuid.uuid4().hex[:8]}"
    p.mkdir(mode=0o700, exist_ok=False)
    try:
        yield p
    finally:
        shutil.rmtree(p, ignore_errors=True)
