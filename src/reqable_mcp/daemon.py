"""Daemon — wires all the in-process components together.

The MVP is single-process: `reqable-mcp serve` starts an MCP stdio
server. Inside that process, this Daemon class owns the LMDB poller
thread, the SQLite cache, the body file reader, and the wait queue.

When Claude Code disconnects from the stdio MCP server, the process
exits and everything tears down. There is no on-disk daemon, no
launchd plist, no IPC socket (those move to Phase 2 if/when we add
the addons.py hook for tag/modify/mock).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import lmdb

from . import proxy_guard
from .db import Database
from .ipc.protocol import Request, ok_response
from .ipc.server import IpcServer
from .paths import Paths, resolve
from .rules import RuleEngine
from .sources.body_source import BodySource
from .sources.lmdb_source import LmdbSource
from .sources.objectbox_meta import Entity, load_schema
from .wait_queue import WaitQueue

log = logging.getLogger(__name__)


@dataclass
class DaemonConfig:
    """Tunables a caller may want to override (mostly for tests)."""

    strict_proxy: bool | None = None
    """``True`` exits if a third-party proxy is detected. ``None``
    falls back to the ``REQABLE_MCP_STRICT_PROXY`` env var."""

    enable_ipc: bool = True
    """Whether to start the Phase 2 IPC server. Tests / cmd_status
    can disable to avoid binding the socket file."""


class Daemon:
    """Holds every long-lived component the MCP tools call into.

    Lifecycle: construct → :meth:`start` (idempotent) → run MCP server
    → :meth:`stop` on shutdown.
    """

    def __init__(
        self,
        paths: Paths | None = None,
        config: DaemonConfig | None = None,
    ):
        self.paths: Paths = paths or resolve()
        self.config: DaemonConfig = config or DaemonConfig()
        self.db: Database | None = None
        self.body_source: BodySource | None = None
        self.lmdb_source: LmdbSource | None = None
        self.wait_queue: WaitQueue | None = None
        self.rule_engine: RuleEngine | None = None
        self.ipc_server: IpcServer | None = None
        self.schema: dict[str, Entity] = {}
        self._started = False

    # ------------------------------------------------------------------ start

    def start(self) -> None:
        """Bring up everything. Safe to call twice."""
        if self._started:
            return

        # Strong constraint: scrub proxy env BEFORE opening anything.
        proxy_guard.assert_proxy_safe(strict=self.config.strict_proxy)

        self.paths.assert_reqable_present()
        self.paths.ensure_our_dirs()

        # Load schema once at startup. If Reqable changes its model,
        # users restart the MCP server and we re-introspect.
        env = lmdb.open(
            str(self.paths.reqable_lmdb_dir),
            readonly=True,
            lock=False,
            max_dbs=64,
            subdir=True,
            create=False,
        )
        try:
            self.schema = load_schema(env)
        finally:
            env.close()

        if "CaptureRecordHistoryEntity" not in self.schema:
            raise RuntimeError(
                "Reqable LMDB has no CaptureRecordHistoryEntity — "
                "open Reqable and capture some traffic first."
            )

        self.db = Database(self.paths.our_cache_db)
        self.db.init_schema()

        self.body_source = BodySource(self.paths.reqable_capture_dir)
        self.wait_queue = WaitQueue()

        # Wrap notify so its int return doesn't violate the
        # ``Callable[[dict], None]`` contract LmdbSource expects.
        wq = self.wait_queue

        def _notify_waiters(rec: dict) -> None:
            wq.notify(rec)

        self.lmdb_source = LmdbSource(
            self.paths.reqable_lmdb_dir,
            self.db,
            self.schema,
            on_new_capture=_notify_waiters,
        )
        self.lmdb_source.start()

        # Phase 2: rule engine (always loaded so MCP tools can list/clear
        # rules even without IPC; addons can't talk to us without IPC).
        self.rule_engine = RuleEngine(self.paths.our_rules_json)
        self.rule_engine.load()

        if self.config.enable_ipc:
            self.ipc_server = IpcServer(
                self.paths.our_socket, self._handle_ipc_request
            )
            self.ipc_server.start()

        self._started = True
        log.info(
            "reqable-mcp daemon started — cache=%s lmdb=%s ipc=%s",
            self.paths.our_cache_db,
            self.paths.reqable_lmdb_dir,
            self.paths.our_socket if self.config.enable_ipc else "disabled",
        )

    # ------------------------------------------------------------------ stop

    def stop(self) -> None:
        if not self._started:
            return
        if self.ipc_server is not None:
            try:
                self.ipc_server.stop()
            except Exception:
                log.exception("ipc_server.stop failed")
        if self.lmdb_source is not None:
            try:
                self.lmdb_source.stop()
            except Exception:
                log.exception("lmdb_source.stop failed")
        self._started = False
        log.info("reqable-mcp daemon stopped")

    # ------------------------------------------------------------------ ipc

    def _handle_ipc_request(self, req: Request) -> bytes:
        """Dispatch an addons.py IPC request.

        Verbs:
          * ``get_rules`` — return rules to apply for one in-flight
            request/response. args: ``{side, host, path, method}``.
          * ``report_hit`` — addons confirms it applied a rule.
            args: ``{rule_ids: [...]}``.

        Unknown verbs return a 4xx-equivalent (``ok=false``).
        """
        from .ipc.protocol import error_response  # avoid circular at import

        if self.rule_engine is None:
            return error_response("daemon not fully started")

        if req.op == "get_rules":
            args = req.args
            rules = self.rule_engine.match_for(
                side=args.get("side", ""),
                host=args.get("host"),
                path=args.get("path"),
                method=args.get("method"),
            )
            return ok_response([r.to_addon_payload() for r in rules])

        if req.op == "report_hit":
            for rid in req.args.get("rule_ids") or []:
                self.rule_engine.record_hit(str(rid))
            return ok_response({})

        return error_response(f"unknown op: {req.op}")

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        """Snapshot for the ``status`` CLI / MCP tool."""
        st = self.lmdb_source.stats if self.lmdb_source else None
        return {
            "started": self._started,
            "lmdb_path": str(self.paths.reqable_lmdb_dir),
            "capture_dir": str(self.paths.reqable_capture_dir),
            "cache_db": str(self.paths.our_cache_db),
            "active_waiters": self.wait_queue.active_count() if self.wait_queue else 0,
            "lmdb_stats": (
                {
                    "polls": st.polls,
                    "new_records": st.new_records,
                    "decode_failures": st.decode_failures,
                    "last_seen_ob_id": st.last_seen_ob_id,
                    "last_poll_ts_ms": st.last_poll_ts_ms,
                }
                if st is not None
                else None
            ),
            "schema_entities": sorted(self.schema.keys()) if self.schema else [],
            "rules": self.rule_engine.stats() if self.rule_engine else None,
            "ipc": self.ipc_server.stats() if self.ipc_server else None,
        }


__all__ = ["Daemon", "DaemonConfig"]
