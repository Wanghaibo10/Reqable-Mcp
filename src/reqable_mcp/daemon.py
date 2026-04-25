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

import json
import logging
import threading
from dataclasses import dataclass

import lmdb

from . import proxy_guard
from .db import Database
from .dry_run import DryRunLog
from .ipc.protocol import MAX_MESSAGE_BYTES, Request, error_response, ok_response
from .ipc.server import IpcServer
from .paths import Paths, resolve
from .relay import MAX_RELAY_TTL_SECONDS, MAX_RELAY_VALUE_BYTES, RelayStore
from .rules import Rule, RuleEngine
from .sources.body_source import BodySource
from .sources.lmdb_source import LmdbSource
from .sources.objectbox_meta import Entity, load_schema
from .wait_queue import WaitQueue

# Reserve some headroom under the IPC frame cap for the wrapper JSON
# (``{"ok":true,"data":[...]}\n``) and any commas/whitespace.
# 4 KB is plenty for a tiny wrapper.
_IPC_RULE_PAYLOAD_BUDGET: int = MAX_MESSAGE_BYTES - 4096

# How often the background reaper drops expired rules from the engine.
# Expired rules don't affect routing (``match_for`` filters by ``now``),
# but they sit in memory and in ``rules.json`` until something prunes
# them. Running this every 30s keeps the file small without wasting CPU.
RULE_REAP_INTERVAL_S: float = 30.0

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

    reap_interval_seconds: float = RULE_REAP_INTERVAL_S
    """How often the rule reaper runs. Override in tests to trigger
    a sweep within a reasonable wait time."""


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
        self.relay_store: RelayStore | None = None
        self.dry_run_log: DryRunLog | None = None
        self.ipc_server: IpcServer | None = None
        self.schema: dict[str, Entity] = {}
        self._started = False
        self._reaper_stop = threading.Event()
        self._reaper_thread: threading.Thread | None = None

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
        # RuleEngine auto-loads on construction.
        self.rule_engine = RuleEngine(self.paths.our_rules_json)
        # Phase 3: relay store backs auto_token_relay. Volatile by
        # design — restart wipes any stored tokens.
        self.relay_store = RelayStore()
        # Phase 4: dry-run feedback log (volatile, ring-buffered).
        self.dry_run_log = DryRunLog()

        if self.config.enable_ipc:
            self.ipc_server = IpcServer(
                self.paths.our_socket, self._handle_ipc_request
            )
            self.ipc_server.start()

        # Start the rule reaper. ``match_for`` already ignores expired
        # rules, but pruning them periodically keeps memory + the
        # persisted rules.json from growing unbounded over a long-lived
        # daemon. Daemon thread so it dies with the process.
        self._reaper_stop.clear()
        self._reaper_thread = threading.Thread(
            target=self._reap_loop,
            name="reqable-mcp-rule-reaper",
            daemon=True,
        )
        self._reaper_thread.start()

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
        # Stop the reaper first; it never blocks long, but joining
        # before tearing down rule_engine avoids a benign race.
        self._reaper_stop.set()
        if self._reaper_thread is not None:
            self._reaper_thread.join(timeout=2.0)
            self._reaper_thread = None
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

    def _reap_loop(self) -> None:
        """Background loop: prune expired rules and relay tokens every
        :data:`RULE_REAP_INTERVAL_S` seconds. Stops when ``stop()``
        sets ``_reaper_stop`` (we use ``Event.wait`` so shutdown is
        responsive instead of sleeping the full interval).
        """
        interval = self.config.reap_interval_seconds
        while not self._reaper_stop.wait(timeout=interval):
            if self.rule_engine is not None:
                try:
                    dropped = self.rule_engine.reap_expired()
                except Exception:
                    log.exception("rule reaper failed")
                else:
                    if dropped:
                        log.info("rule reaper dropped %d expired rule(s)", dropped)
            if self.relay_store is not None:
                try:
                    rel_dropped = self.relay_store.reap_expired()
                except Exception:
                    log.exception("relay reaper failed")
                else:
                    if rel_dropped:
                        log.info("relay reaper dropped %d expired token(s)", rel_dropped)

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
            payloads = self._pack_rules_for_ipc(rules, args)
            return ok_response(payloads)

        if req.op == "report_hit":
            for rid in req.args.get("rule_ids") or []:
                self.rule_engine.record_hit(str(rid))
            return ok_response({})

        if req.op == "store_relay_value":
            return self._handle_store_relay(req.args)

        if req.op == "get_relay_value":
            return self._handle_get_relay(req.args)

        if req.op == "report_dry_run":
            return self._handle_report_dry_run(req.args)

        return error_response(f"unknown op: {req.op}")

    def _handle_report_dry_run(self, args: dict) -> bytes:
        if self.dry_run_log is None:
            return error_response("dry-run log not started")
        rule_id = args.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id:
            return error_response("rule_id must be a non-empty string")
        # Coerce to strings; addons may report unusual shapes.
        self.dry_run_log.record(
            rule_id=rule_id,
            uid=str(args.get("uid", "")),
            host=str(args.get("host", "")),
            path=str(args.get("path", "")),
            method=str(args.get("method", "")),
            side=str(args.get("side", "")),
        )
        return ok_response({"recorded": True})

    def _handle_store_relay(self, args: dict) -> bytes:
        if self.relay_store is None:
            return error_response("relay store not started")
        name = args.get("name")
        value = args.get("value")
        ttl = args.get("ttl_seconds", 300)
        if not isinstance(name, str) or not name:
            return error_response("name must be a non-empty string")
        if not isinstance(value, str):
            return error_response("value must be a string")
        if not isinstance(ttl, int) or not (0 < ttl <= MAX_RELAY_TTL_SECONDS):
            return error_response(
                f"ttl_seconds must be int in (0, {MAX_RELAY_TTL_SECONDS}]"
            )
        if len(value.encode("utf-8")) > MAX_RELAY_VALUE_BYTES:
            return error_response(
                f"value exceeds MAX_RELAY_VALUE_BYTES={MAX_RELAY_VALUE_BYTES}"
            )
        try:
            self.relay_store.set(name, value, ttl_seconds=ttl)
        except (TypeError, ValueError) as e:
            return error_response(str(e))
        return ok_response({"stored": True})

    def _handle_get_relay(self, args: dict) -> bytes:
        if self.relay_store is None:
            return error_response("relay store not started")
        name = args.get("name")
        if not isinstance(name, str) or not name:
            return error_response("name must be a non-empty string")
        value = self.relay_store.get(name)
        return ok_response({"value": value})

    @staticmethod
    def _pack_rules_for_ipc(
        rules: list[Rule], req_args: dict
    ) -> list[dict]:
        """Pack matched rules into a payload that fits the IPC frame cap.

        The naive ``[r.to_addon_payload() for r in rules]`` can exceed
        :data:`MAX_MESSAGE_BYTES` if multiple large ``replace_body`` /
        ``mock`` rules match the same request. When that happens
        :func:`encode_message` raises and the addons side fails open,
        which silently disables every other rule — including ``block``.

        Strategy:
        * always include every ``block`` rule (their payload is empty)
        * order the rest by individual JSON size, smallest first, so a
          single oversized rule can't crowd out many small ones
        * stop once the running JSON size hits the budget

        Drops are logged so an operator can see what got squeezed out.
        """
        encoded = [
            (r, r.to_addon_payload(), len(json.dumps(r.to_addon_payload(), ensure_ascii=False)))
            for r in rules
        ]
        # block-first, then ascending size.
        encoded.sort(key=lambda t: (0 if t[0].kind == "block" else 1, t[2]))

        out: list[dict] = []
        used = 2  # the surrounding "[]"
        dropped: list[Rule] = []
        for rule, payload, size in encoded:
            # +1 for the comma separator after the previous element.
            cost = size + (1 if out else 0)
            if used + cost > _IPC_RULE_PAYLOAD_BUDGET:
                dropped.append(rule)
                continue
            out.append(payload)
            used += cost
        if dropped:
            log.warning(
                "ipc get_rules: dropped %d rule(s) to fit %d-byte budget "
                "(matched=%d, host=%r, path=%r, side=%r); kept %d",
                len(dropped),
                _IPC_RULE_PAYLOAD_BUDGET,
                len(rules),
                req_args.get("host"),
                req_args.get("path"),
                req_args.get("side"),
                len(out),
            )
        return out

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
            "relay": self.relay_store.stats() if self.relay_store else None,
            "dry_run": self.dry_run_log.stats() if self.dry_run_log else None,
            "ipc": self.ipc_server.stats() if self.ipc_server else None,
        }


__all__ = ["Daemon", "DaemonConfig"]
