"""CLI entry point: ``reqable-mcp <command>``.

Commands
--------
* ``serve``         — run the MCP stdio server (Claude Code spawns this)
* ``status``        — print daemon status as JSON, then exit
* ``install-help``  — print Claude Code MCP registration JSON snippet
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__, proxy_guard
from .daemon import Daemon, DaemonConfig
from .mcp_server import run_stdio, set_daemon


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        # MCP stdio uses stdout; logs go to stderr.
        stream=sys.stderr,
    )


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the daemon and block in the MCP stdio loop."""
    daemon = Daemon(config=DaemonConfig(strict_proxy=args.strict_proxy))
    daemon.start()
    set_daemon(daemon)
    try:
        run_stdio()
    finally:
        daemon.stop()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print daemon status as JSON and exit (no stdio loop).

    For status we drive ``scan_once`` synchronously to fill counters
    and avoid spawning the background poller thread (which would race
    with us on SQLite writes).
    """
    proxy_guard.assert_proxy_safe(strict=args.strict_proxy)

    # Manual lightweight init — same as Daemon.start() but skips
    # `lmdb_source.start()` so no background thread.
    import lmdb

    from .db import Database
    from .sources.body_source import BodySource
    from .sources.lmdb_source import LmdbSource
    from .sources.objectbox_meta import load_schema
    from .wait_queue import WaitQueue

    daemon = Daemon(config=DaemonConfig(strict_proxy=args.strict_proxy))
    daemon.paths.assert_reqable_present()
    daemon.paths.ensure_our_dirs()
    env = lmdb.open(
        str(daemon.paths.reqable_lmdb_dir),
        readonly=True, lock=False, max_dbs=64, subdir=True, create=False,
    )
    try:
        daemon.schema = load_schema(env)
    finally:
        env.close()

    daemon.db = Database(daemon.paths.our_cache_db)
    daemon.db.init_schema()
    daemon.body_source = BodySource(daemon.paths.reqable_capture_dir)
    daemon.wait_queue = WaitQueue()
    daemon.lmdb_source = LmdbSource(
        daemon.paths.reqable_lmdb_dir, daemon.db, daemon.schema
    )
    daemon._started = True

    daemon.lmdb_source.scan_once()
    print(json.dumps(daemon.status(), indent=2, default=str))
    return 0


def cmd_install_help(_: argparse.Namespace) -> int:
    """Emit the MCP server snippet for ~/.claude/mcp.json."""
    snippet = {
        "mcpServers": {
            "reqable": {
                "command": "reqable-mcp",
                "args": ["serve"],
            }
        }
    }
    print("Add the following to ~/.claude/mcp.json (or settings.json):")
    print()
    print(json.dumps(snippet, indent=2))
    print()
    print(
        "Then restart Claude Code. Verify with `/mcp` — `reqable` should "
        "show as connected."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reqable-mcp",
        description="Local MCP server for Reqable's captured HTTP traffic.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging level on stderr (default: INFO)",
    )
    parser.add_argument(
        "--strict-proxy",
        action="store_true",
        default=None,
        help="exit if a non-loopback system proxy is detected; "
        "default reads REQABLE_MCP_STRICT_PROXY env var",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the MCP stdio server").set_defaults(
        func=cmd_serve
    )
    sub.add_parser("status", help="print daemon status as JSON").set_defaults(
        func=cmd_status
    )
    sub.add_parser(
        "install-help", help="show MCP registration JSON for Claude Code"
    ).set_defaults(func=cmd_install_help)

    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
