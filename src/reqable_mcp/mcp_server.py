"""FastMCP stdio server.

This module is the single seam between the rest of the codebase and
the MCP SDK. It registers tool functions (delegating to ``tools/``
modules) and runs the stdio transport.

Tools are registered against a module-level ``mcp`` instance and bind
to a globally-installed :class:`Daemon`. The daemon is set by
``main()`` before tools are invoked, so module-import order doesn't
matter.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from .daemon import Daemon

log = logging.getLogger(__name__)

# Module-level singletons. Tools imported from `tools/` reference these.
mcp: FastMCP = FastMCP("reqable")
_daemon: Daemon | None = None


def set_daemon(d: Daemon) -> None:
    """Install the daemon for tool functions to use."""
    global _daemon
    _daemon = d


def get_daemon() -> Daemon:
    """Return the live daemon. Raises if not yet installed."""
    if _daemon is None:
        raise RuntimeError("daemon not initialized — call set_daemon() first")
    return _daemon


# ---------------------------------------------------------------- built-in tools


@mcp.tool()
def status() -> dict[str, Any]:
    """Get reqable-mcp daemon status, schema, and live counters.

    Useful as a smoke test from Claude Code: if this returns, the
    server is up and the LMDB poller is healthy.
    """
    return get_daemon().status()


# Tools from tools/* will register themselves on the same `mcp`
# instance via `from .mcp_server import mcp` at import time.
# We import them here to ensure they get loaded before run().


def _import_all_tools() -> None:
    """Trigger registration of every tool module.

    Imports are deferred so we can ``set_daemon`` first; FastMCP's
    decorators execute during import.
    """
    # Tier 1 / 4 / 5 will be added in the corresponding milestones.
    # Imports are wrapped in try/except so partially-implemented
    # tools/ modules don't keep the server from starting in dev.
    for mod_name in (
        "reqable_mcp.tools.query",
        "reqable_mcp.tools.wait",
        "reqable_mcp.tools.analysis",
    ):
        try:
            __import__(mod_name)
        except Exception:
            # Don't silently swallow — a missing tool module means the
            # user gets a degraded server with no warning. Surface at
            # warning level so it shows in the default log config.
            log.warning(
                "failed to load tool module %s; tools from it will be "
                "unavailable", mod_name, exc_info=True,
            )


def run_stdio() -> None:
    """Block in the stdio MCP main loop.

    Caller must have already called :func:`set_daemon` and started it.
    """
    _import_all_tools()
    mcp.run(transport="stdio")


__all__ = ["mcp", "set_daemon", "get_daemon", "run_stdio"]
