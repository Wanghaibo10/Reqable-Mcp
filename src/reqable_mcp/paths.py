"""Filesystem path constants and resolution.

Centralizes every path the daemon touches so swap-outs (alternative
Reqable install location, sandboxed test fixtures) only modify here.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from pathlib import Path

# Reqable's macOS bundle support directory. Stable since Reqable 2.x.
REQABLE_BUNDLE_ID = "com.reqable.macosx"

DEFAULT_REQABLE_SUPPORT = (
    Path.home() / "Library" / "Application Support" / REQABLE_BUNDLE_ID
)
DEFAULT_OUR_DATA = Path.home() / ".reqable-mcp"


@dataclass(frozen=True)
class Paths:
    """All resolved paths for one daemon instance.

    Storage layout discovered on Reqable 3.0.40 (2026-04):

    * ``box/data.mdb`` — ObjectBox/LMDB; ``CaptureRecordHistoryEntity``
      keyed under prefix ``\\x18\\x00\\x00\\x2c`` holds metadata
      (method/url/host/headers/timing/appInfo) for every captured
      request. body is NOT in here.
    * ``capture/`` — raw request and response bodies, named
      ``{conn.timestamp}-{conn.id}-{session.id}-{req_raw|res-raw|res-extract}-body.reqable``.
      Linked back to LMDB records via the matching session.connection
      fields. Reqable maintains this directory; we only read.
    * ``rest/`` — REST API client history bodies (Postman-like
      feature). Unrelated to mitm capture; not used by us.
    """

    reqable_support: Path
    reqable_lmdb_dir: Path  # box/, contains data.mdb / lock.mdb
    reqable_capture_dir: Path  # capture/, contains *-{req_raw|res-raw|res-extract}-body.reqable
    reqable_config_dir: Path
    reqable_capture_config: Path  # JSON file for scriptConfig (Phase 2 only)
    our_data: Path
    our_cache_db: Path
    our_log: Path
    our_state_json: Path
    our_socket: Path  # Phase 2 IPC socket

    def assert_reqable_present(self) -> None:
        """Raise FileNotFoundError if Reqable's data dir is missing.

        Call this at startup. We tolerate missing rest/ at runtime
        (it's only needed when fetching raw bodies) but we cannot
        function without the LMDB dir.
        """
        if not self.reqable_support.exists():
            raise FileNotFoundError(
                f"Reqable support dir not found: {self.reqable_support}. "
                "Is Reqable installed and has it been launched at least once?"
            )
        if not (self.reqable_lmdb_dir / "data.mdb").exists():
            raise FileNotFoundError(
                f"Reqable LMDB not found: {self.reqable_lmdb_dir}/data.mdb. "
                "Has Reqable captured any traffic yet?"
            )

    def ensure_our_dirs(self) -> None:
        """Create our data dir tree with restrictive perms (0700)."""
        self.our_data.mkdir(mode=0o700, exist_ok=True, parents=True)
        # Tighten if it pre-existed with looser perms
        with contextlib.suppress(PermissionError):
            os.chmod(self.our_data, 0o700)


def resolve(
    reqable_support: Path | None = None,
    our_data: Path | None = None,
) -> Paths:
    """Resolve all paths.

    Both arguments default to user-level dirs. Tests pass tmp paths.
    """
    rs = (reqable_support or DEFAULT_REQABLE_SUPPORT).expanduser().resolve()
    od = (our_data or DEFAULT_OUR_DATA).expanduser().resolve()

    return Paths(
        reqable_support=rs,
        reqable_lmdb_dir=rs / "box",
        reqable_capture_dir=rs / "capture",
        reqable_config_dir=rs / "config",
        reqable_capture_config=rs / "config" / "capture_config",
        our_data=od,
        our_cache_db=od / "cache.db",
        our_log=od / "daemon.log",
        our_state_json=od / "state.json",
        our_socket=od / "daemon.sock",
    )
