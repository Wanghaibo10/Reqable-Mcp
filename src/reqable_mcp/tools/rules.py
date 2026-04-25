"""Tier-2 / Tier-3 MCP tools — rule management for the Reqable hook.

These let an LLM install / inspect / revoke rules that the Phase-2
addons.py applies to live traffic. All rules carry a TTL (default
300s, max 3600s) so a forgotten rule cannot silently rewrite traffic
forever — see ``rules.py`` for the engine + persistence.

Rule kinds exposed here:

* ``tag_pattern``      → highlight matching captures (red/yellow/...)
* ``comment_pattern``  → attach a free-form note
* ``inject_header``    → add or override a request/response header
* ``list_rules``       → introspect active rules
* ``remove_rule``      → revoke one rule by id
* ``clear_rules``      → revoke everything (panic button)

Mock / block / replace_body live in a separate Tier-3 module (M15) so
the safer surface here can ship first.

Workflow note: rules persist to ``~/.reqable-mcp/rules.json``. They
remain after the MCP server shuts down — the next start auto-loads
them (and drops anything already expired).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from ..mcp_server import get_daemon, mcp
from ..rules import DEFAULT_TTL_SECONDS, MAX_TTL_SECONDS, Rule

log = logging.getLogger(__name__)

# Whitelist of color names addons.py understands.
_VALID_COLORS = ("red", "yellow", "green", "blue", "teal", "strikethrough")


def _serialize_rule(r: Rule) -> dict[str, Any]:
    """Public-facing rule shape for ``list_rules`` etc."""
    return {
        "id": r.id,
        "kind": r.kind,
        "side": r.side,
        "host": r.host,
        "path_pattern": r.path_pattern,
        "method": r.method,
        "payload": r.payload,
        "created_ts": r.created_ts,
        "expires_ts": r.expires_ts,
        "hits": r.hits,
    }


def _engine_or_error() -> Any:
    """Helper: return rule engine or an error dict for tools to short-circuit."""
    daemon = get_daemon()
    if daemon.rule_engine is None:
        return None
    return daemon.rule_engine


# ---------------------------------------------------------------- tag_pattern


@mcp.tool()
def tag_pattern(
    host: str | None = None,
    path_pattern: str | None = None,
    method: str | None = None,
    color: str = "red",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Highlight matching captures in Reqable's UI with a color.

    Use to spotlight requests of interest as they happen — e.g. tag
    every POST to ``api.example.com/login`` red so they stand out in
    the capture list.

    Filters are AND-combined; ``None`` means "any". ``path_pattern``
    is a Python regex applied to the request path with ``re.search``.

    Colors: ``red`` / ``yellow`` / ``green`` / ``blue`` / ``teal`` /
    ``strikethrough``.

    Returns ``{rule_id, expires_at}`` on success, or ``{error: ...}``.
    """
    if color not in _VALID_COLORS:
        return {"error": f"color must be one of {_VALID_COLORS}, got {color!r}"}
    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}
    try:
        rule = engine.add(
            kind="tag",
            side="request",
            host=host,
            path_pattern=path_pattern,
            method=method,
            payload={"color": color},
            ttl_seconds=ttl_seconds,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"rule_id": rule.id, "expires_at": rule.expires_ts}


# ---------------------------------------------------------------- comment_pattern


@mcp.tool()
def comment_pattern(
    text: str,
    host: str | None = None,
    path_pattern: str | None = None,
    method: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Attach a free-form comment to matching captures.

    Useful as a poor man's annotation when working through a flow:
    ``comment_pattern(text="step 2 token request", host="api.x.com",
    path_pattern="/oauth/token")`` then later ``list_recent`` shows
    those captures with the comment attached in the LMDB record.
    """
    if not text or len(text) > 500:
        return {"error": "text must be a non-empty string up to 500 chars"}
    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}
    try:
        rule = engine.add(
            kind="comment",
            side="request",
            host=host,
            path_pattern=path_pattern,
            method=method,
            payload={"text": text},
            ttl_seconds=ttl_seconds,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"rule_id": rule.id, "expires_at": rule.expires_ts}


# ---------------------------------------------------------------- inject_header


@mcp.tool()
def inject_header(
    name: str,
    value: str,
    host: str | None = None,
    path_pattern: str | None = None,
    method: str | None = None,
    side: Literal["request", "response"] = "request",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Add or override a header on matching live traffic.

    **This rewrites real traffic** — pass ``ttl_seconds`` shorter
    rather than longer if you only need a few minutes. Max
    ``ttl_seconds`` is 3600.

    ``side="request"`` (default) injects on outbound requests;
    ``side="response"`` on inbound responses (visible to the
    requesting client only — the upstream server doesn't see it).

    If the header name already exists on the message, this overwrites it.
    """
    if not name or not isinstance(name, str):
        return {"error": "name must be a non-empty string"}
    if not isinstance(value, str):
        return {"error": "value must be a string"}
    if name.startswith(":"):
        return {"error": "cannot inject pseudo-headers (h2 :method/:path/etc.)"}
    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}
    try:
        rule = engine.add(
            kind="inject_header",
            side=side,
            host=host,
            path_pattern=path_pattern,
            method=method,
            payload={"name": name, "value": value},
            ttl_seconds=ttl_seconds,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"rule_id": rule.id, "expires_at": rule.expires_ts}


# ---------------------------------------------------------------- list_rules


@mcp.tool()
def list_rules(kind: str | None = None) -> list[dict[str, Any]]:
    """List currently-active rules with hit counts.

    Filter by ``kind`` (one of ``tag`` / ``comment`` / ``inject_header``
    / ``replace_body`` / ``mock`` / ``block``). Expired rules are not
    returned.
    """
    engine = _engine_or_error()
    if engine is None:
        return []
    return [_serialize_rule(r) for r in engine.list_all(kind=kind)]


# ---------------------------------------------------------------- remove_rule


@mcp.tool()
def remove_rule(rule_id: str) -> dict[str, Any]:
    """Revoke one rule by id. Returns ``{removed: bool}``."""
    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available"}
    return {"removed": engine.remove(rule_id)}


# ---------------------------------------------------------------- clear_rules


@mcp.tool()
def clear_rules() -> dict[str, Any]:
    """Revoke ALL rules. Use as a panic button when something goes wrong.

    Returns ``{cleared: int}`` with the count removed.
    """
    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available"}
    return {"cleared": engine.clear()}


# ---------------------------------------------------------------- ttl_limits


@mcp.tool()
def ttl_limits() -> dict[str, int]:
    """Return ``{default, max}`` TTL seconds the engine accepts.

    Lets the LLM check the bounds before installing a rule that
    would otherwise be rejected.
    """
    return {"default": DEFAULT_TTL_SECONDS, "max": MAX_TTL_SECONDS}


__all__: list[str] = []  # tools register themselves via @mcp.tool
