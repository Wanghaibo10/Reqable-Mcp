"""Tier-4 ``wait_for`` tool.

Lets a Claude Code conversation block until the next capture matching
a filter spec. Typical use: "user, click the login button now — I'll
wait for ``auth.api/login`` and analyze the response".

The match comes from the LmdbSource poller noticing a new record and
invoking WaitQueue.notify; this tool function blocks on the queue and
returns the matched capture (or ``None`` on timeout).
"""

from __future__ import annotations

from typing import Any

from ..mcp_server import get_daemon, mcp
from ..wait_queue import FilterSpec


@mcp.tool()
def wait_for(
    host: str | None = None,
    path_pattern: str | None = None,
    method: str | None = None,
    app: str | None = None,
    status: int | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any] | None:
    """Block until a capture matches the given filter, or until timeout.

    Filter conditions are AND-combined. ``path_pattern`` is a Python
    regex applied to the full URL via ``re.search`` (so it matches
    either path-or-URL naturally). All other filters are exact match.

    Returns the matched capture (same shape as ``list_recent`` row) or
    ``None`` if timeout elapsed first.

    Example use:
      ``wait_for(host="api.example.com", method="POST", timeout_seconds=60)``
    """
    daemon = get_daemon()
    if daemon.wait_queue is None:
        return None

    try:
        spec = FilterSpec(
            host=host,
            method=method,
            path_pattern=path_pattern,
            app=app,
            status=status,
        )
    except ValueError as e:
        # Invalid regex etc. — surface as tool error.
        return {"error": str(e)}

    waiter_id = daemon.wait_queue.add(spec)
    try:
        # Cap timeout at 5 minutes to keep a stuck wait from
        # holding the MCP transport indefinitely.
        capped = max(0.0, min(float(timeout_seconds), 300.0))
        return daemon.wait_queue.wait(waiter_id, timeout=capped)
    finally:
        daemon.wait_queue.cancel(waiter_id)


__all__: list[str] = []
