"""Tier-2 / Tier-3 MCP tools — rule management for the Reqable hook.

These let an LLM install / inspect / revoke rules that the Phase-2
addons.py applies to live traffic. All rules carry a TTL (default
300s, max 3600s) so a forgotten rule cannot silently rewrite traffic
forever — see ``rules.py`` for the engine + persistence.

Rule kinds exposed here:

Tier 2 (safe-ish — annotate or add to traffic, doesn't drop or replace):
* ``tag_pattern``      → highlight matching captures (red/yellow/...)
* ``comment_pattern``  → attach a free-form note
* ``inject_header``    → add or override a request/response header

Tier 3 (rewrites or kills traffic — read the docstrings):
* ``replace_body``     → swap an entire request/response body
* ``mock_response``    → fake the upstream's response (upstream still hit!)
* ``block_request``    → abort the request before it reaches upstream

Plumbing:
* ``list_rules``       → introspect active rules
* ``remove_rule``      → revoke one rule by id
* ``clear_rules``      → revoke everything (panic button)
* ``ttl_limits``       → read the TTL bounds the engine accepts

Workflow note: rules persist to ``~/.reqable-mcp/rules.json``. They
remain after the MCP server shuts down — the next start auto-loads
them (and drops anything already expired).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from ..mcp_server import get_daemon, mcp
from ..rules import BODY_MAX_BYTES, DEFAULT_TTL_SECONDS, MAX_TTL_SECONDS, Rule

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
    """Return ``{default, max, body_max_bytes}`` the engine accepts.

    Lets the LLM check the bounds before installing a rule that
    would otherwise be rejected.
    """
    return {
        "default": DEFAULT_TTL_SECONDS,
        "max": MAX_TTL_SECONDS,
        "body_max_bytes": BODY_MAX_BYTES,
    }


# ---------------------------------------------------------------- replace_body
# Tier 3 — rewrites real traffic.


def _coerce_body(body: Any) -> tuple[Any, str | None]:
    """Validate a body payload destined for an addons rule.

    Returns ``(coerced, error)``. On success ``error`` is None.
    The IPC channel is JSON so only ``str`` / ``dict`` survive the
    round trip. Bytes / lists / numbers are rejected up-front so the
    rule never makes it down to addons in a shape it can't apply.

    Strings carrying lone surrogates (e.g. ``"\\ud800"``) cannot be
    encoded to UTF-8 — we catch ``UnicodeEncodeError`` and surface a
    clean error rather than letting an exception bubble out of a tool.
    Same for dicts that contain such strings.
    """
    if isinstance(body, str):
        try:
            size = len(body.encode("utf-8"))
        except UnicodeEncodeError as e:
            return None, f"body string not UTF-8 encodable: {e}"
        if size > BODY_MAX_BYTES:
            return None, f"body exceeds BODY_MAX_BYTES={BODY_MAX_BYTES}"
        return body, None
    if isinstance(body, dict):
        try:
            encoded = json.dumps(body, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            return None, f"body dict not JSON-serializable: {e}"
        try:
            size = len(encoded.encode("utf-8"))
        except UnicodeEncodeError as e:
            return None, f"body dict not UTF-8 encodable: {e}"
        if size > BODY_MAX_BYTES:
            return None, f"body exceeds BODY_MAX_BYTES={BODY_MAX_BYTES}"
        return body, None
    return None, (
        f"body must be a str or dict; got {type(body).__name__}. "
        "Binary bodies are not supported (IPC is JSON)."
    )


# Patterns that are *technically* regexes but match every path. Allowing
# any of these inside a ``block_request`` filter would let the rule
# silently kill all traffic — same outcome as no filter at all.
_CATCHALL_PATTERNS: frozenset[str] = frozenset(
    ("", ".*", ".+", ".*?", ".+?", "^", "^.*", "^.+", "^.*$", "^.+$")
)


def _is_specified(value: str | None) -> bool:
    """``None`` and empty-string both mean "no filter"."""
    return value is not None and value != ""


@mcp.tool()
def replace_body(
    body: str | dict[str, Any],
    host: str | None = None,
    path_pattern: str | None = None,
    method: str | None = None,
    side: Literal["request", "response"] = "request",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Swap the entire request/response body on matching live traffic.

    **This rewrites real traffic.** ``side="request"`` replaces the
    outbound body before Reqable forwards it to the upstream;
    ``side="response"`` replaces what the requesting client receives
    (the upstream still saw / sent the original).

    ``body`` may be a ``str`` (sent as-is) or a ``dict`` (Reqable
    json.dumps'es it server-side). Binary bodies are not supported
    — the IPC channel is JSON. Max size: ``BODY_MAX_BYTES`` (64 KB);
    use ``ttl_limits`` to check.

    Pair with a tight ``host`` / ``path_pattern`` so this doesn't
    catch unrelated traffic. TTL defaults to 300s; max 3600s.

    Returns ``{rule_id, expires_at}`` or ``{error}``.
    """
    coerced, err = _coerce_body(body)
    if err is not None:
        return {"error": err}
    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}
    try:
        rule = engine.add(
            kind="replace_body",
            side=side,
            host=host,
            path_pattern=path_pattern,
            method=method,
            payload={"body": coerced},
            ttl_seconds=ttl_seconds,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"rule_id": rule.id, "expires_at": rule.expires_ts}


# ---------------------------------------------------------------- mock_response
# Tier 3 — fakes the response, but does NOT prevent the upstream call.


@mcp.tool()
def mock_response(
    status: int | None = None,
    body: str | dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    host: str | None = None,
    path_pattern: str | None = None,
    method: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Fake the response the client sees — **upstream is still hit**.

    Reqable's addons API does not let us short-circuit a request
    from ``onRequest`` and synthesize a reply. The upstream server
    is contacted and its response arrives normally; we then rewrite
    ``status`` / ``headers`` / ``body`` before the client sees it.
    Side-effects on the upstream (writes, rate limits, billing) are
    NOT prevented. If you need to truly suppress the call, use
    ``block_request`` instead.

    At least one of ``status`` / ``body`` / ``headers`` must be set.
    ``status`` is an int 100–600. ``body`` is str or dict (≤64 KB).
    ``headers`` is a flat ``{name: value}`` dict — overwrites if the
    name exists, otherwise appends.

    Filters (``host`` / ``path_pattern`` / ``method``) and ``ttl_seconds``
    behave the same as ``inject_header``.
    """
    payload: dict[str, Any] = {}
    if status is not None:
        if not isinstance(status, int) or not (100 <= status <= 600):
            return {"error": f"status must be int 100-600, got {status!r}"}
        payload["status"] = status
    if body is not None:
        coerced, err = _coerce_body(body)
        if err is not None:
            return {"error": err}
        payload["body"] = coerced
    if headers is not None:
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in headers.items()
        ):
            return {"error": "headers must be a dict[str, str]"}
        if not headers:
            return {
                "error": (
                    "headers={} would install a no-op rule; pass at least "
                    "one header or omit the argument"
                )
            }
        if any(not k for k in headers):
            return {"error": "header names must be non-empty"}
        if any(k.startswith(":") for k in headers):
            return {"error": "cannot mock pseudo-headers (h2 :method/:path/etc.)"}
        payload["headers"] = dict(headers)
    if not payload:
        return {"error": "must specify at least one of status / body / headers"}
    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}
    try:
        rule = engine.add(
            kind="mock",
            side="response",
            host=host,
            path_pattern=path_pattern,
            method=method,
            payload=payload,
            ttl_seconds=ttl_seconds,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"rule_id": rule.id, "expires_at": rule.expires_ts}


# ---------------------------------------------------------------- block_request
# Tier 3 — aborts the request before it reaches upstream.


@mcp.tool()
def block_request(
    host: str | None = None,
    path_pattern: str | None = None,
    method: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Abort matching requests before they reach the upstream.

    The addons hook raises in ``onRequest``, which Reqable catches and
    surfaces as a session abort — the upstream is **never contacted**.
    The requesting client typically sees a connection error / 502.

    This is the right tool when you want to truly suppress a call (vs
    ``mock_response``, which fakes the reply but the upstream still
    runs). Use a narrow filter — at least one of ``host`` /
    ``path_pattern`` / ``method`` should be specified, otherwise this
    will kill **all** outbound traffic from Reqable's MITM until TTL
    expires. The tool refuses to install with all filters None.

    TTL defaults to 300s; max 3600s.
    """
    # An empty string slips past ``is None`` but means the same thing:
    # "match anything." Treat both as unspecified.
    if not (
        _is_specified(host)
        or _is_specified(path_pattern)
        or _is_specified(method)
    ):
        return {
            "error": (
                "block_request requires at least one non-empty filter "
                "(host / path_pattern / method) — refusing to block "
                "ALL traffic. Use clear_rules() if that's truly intended."
            )
        }
    if path_pattern is not None and path_pattern in _CATCHALL_PATTERNS:
        return {
            "error": (
                f"path_pattern={path_pattern!r} matches every path — "
                "use a narrower regex, or rely on host/method filters "
                "to scope the block."
            )
        }
    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}
    try:
        rule = engine.add(
            kind="block",
            side="request",
            host=host,
            path_pattern=path_pattern,
            method=method,
            payload={},
            ttl_seconds=ttl_seconds,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"rule_id": rule.id, "expires_at": rule.expires_ts}


# ---------------------------------------------------------------- auto_token_relay
# Tier 3 — registers two coupled rules to ferry a token from one
# host's response onto a later request to another host.


_VALID_SOURCE_LOCS = ("header", "json_body")


@mcp.tool()
def auto_token_relay(
    source_host: str,
    source_loc: Literal["header", "json_body"],
    source_field: str,
    target_host: str,
    target_header: str,
    name: str | None = None,
    source_path_pattern: str | None = None,
    target_path_pattern: str | None = None,
    value_prefix: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Capture a token from one host's response, inject it on another.

    Common workflow: an LLM watches a login round-trip, sees the
    server return a token in a JSON body, and wants every subsequent
    API call to that backend to carry the token in an ``Authorization``
    header — without manually copy-pasting between captures.

    This installs **two** coupled rules:

    1. ``relay_extract`` on ``source_host`` (response side): pull
       ``source_field`` from either the response headers
       (``source_loc="header"``) or the JSON body
       (``source_loc="json_body"`` — supports dotted paths like
       ``"data.access_token"``) and store it under ``name`` in the
       daemon's volatile relay store.
    2. ``relay_inject`` on ``target_host`` (request side): read the
       stored value, optionally prepend ``value_prefix`` (e.g.
       ``"Bearer "``), and set it as ``target_header`` on the outbound
       request.

    Both rules share ``ttl_seconds`` (default 300, max 3600). The
    relay store itself is volatile — daemon restarts wipe it. If
    ``name`` is omitted we synthesize one from
    ``source_host:source_field`` so two simultaneous relays don't
    collide.

    Returns ``{relay_name, extract_rule_id, inject_rule_id, expires_at}``
    or ``{error}``.
    """
    if not source_host:
        return {"error": "source_host must be non-empty"}
    if not target_host:
        return {"error": "target_host must be non-empty"}
    if source_loc not in _VALID_SOURCE_LOCS:
        return {
            "error": (
                f"source_loc must be one of {_VALID_SOURCE_LOCS}, "
                f"got {source_loc!r}"
            )
        }
    if not isinstance(source_field, str) or not source_field:
        return {"error": "source_field must be a non-empty string"}
    if not isinstance(target_header, str) or not target_header:
        return {"error": "target_header must be a non-empty string"}
    if target_header.startswith(":"):
        return {"error": "cannot inject pseudo-headers (h2 :method/:path/etc.)"}
    if not isinstance(value_prefix, str):
        return {"error": "value_prefix must be a string"}

    relay_name = (
        name
        if (isinstance(name, str) and name)
        else f"{source_host.lower()}:{source_field}"
    )

    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}

    try:
        extract_rule = engine.add(
            kind="relay_extract", side="response",
            host=source_host,
            path_pattern=source_path_pattern,
            payload={
                "name": relay_name,
                "source_loc": source_loc,
                "source_field": source_field,
                "ttl_seconds": ttl_seconds,
            },
            ttl_seconds=ttl_seconds,
        )
    except ValueError as e:
        return {"error": f"extract rule rejected: {e}"}

    try:
        inject_rule = engine.add(
            kind="relay_inject", side="request",
            host=target_host,
            path_pattern=target_path_pattern,
            payload={
                "name": relay_name,
                "target_header": target_header,
                "value_prefix": value_prefix,
            },
            ttl_seconds=ttl_seconds,
        )
    except ValueError as e:
        # Roll back the extract side so we don't leave a half-installed
        # relay quietly storing tokens nobody injects.
        engine.remove(extract_rule.id)
        return {"error": f"inject rule rejected: {e}"}

    return {
        "relay_name": relay_name,
        "extract_rule_id": extract_rule.id,
        "inject_rule_id": inject_rule.id,
        "expires_at": extract_rule.expires_ts,
    }


__all__: list[str] = []  # tools register themselves via @mcp.tool
