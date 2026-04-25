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
import re
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
        "dry_run": r.dry_run,
        "status_min": r.status_min,
        "status_max": r.status_max,
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
    status_min: int | None = None,
    status_max: int | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Highlight matching captures in Reqable's UI with a color.

    Use to spotlight requests of interest as they happen — e.g. tag
    every POST to ``api.example.com/login`` red so they stand out in
    the capture list.

    Filters are AND-combined; ``None`` means "any". ``path_pattern``
    is a Python regex applied to the request path with ``re.search``.

    ``status_min`` / ``status_max`` (response-side only): inclusive
    HTTP status filter. Pass ``status_min=400, status_max=499`` for
    "all 4xx responses red". The rule auto-runs on the response side
    when either bound is set.

    Colors: ``red`` / ``yellow`` / ``green`` / ``blue`` / ``teal`` /
    ``strikethrough``.

    Returns ``{rule_id, expires_at}`` on success, or ``{error: ...}``.
    """
    if color not in _VALID_COLORS:
        return {"error": f"color must be one of {_VALID_COLORS}, got {color!r}"}
    # Status filter implies response-side. tag_pattern is otherwise a
    # request-side rule because ``context.highlight`` writes back from
    # either side and Reqable shows the same color in the list.
    side: Literal["request", "response"] = (
        "response"
        if (status_min is not None or status_max is not None)
        else "request"
    )
    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}
    try:
        rule = engine.add(
            kind="tag",
            side=side,
            host=host,
            path_pattern=path_pattern,
            method=method,
            payload={"color": color},
            ttl_seconds=ttl_seconds,
            status_min=status_min,
            status_max=status_max,
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
    status_min: int | None = None,
    status_max: int | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> dict[str, Any]:
    """Attach a free-form comment to matching captures.

    Useful as a poor man's annotation when working through a flow:
    ``comment_pattern(text="step 2 token request", host="api.x.com",
    path_pattern="/oauth/token")`` then later ``list_recent`` shows
    those captures with the comment attached in the LMDB record.

    Pass ``status_min`` / ``status_max`` to scope the comment to a
    response-status range (e.g. annotate every 5xx).
    """
    if not text or len(text) > 500:
        return {"error": "text must be a non-empty string up to 500 chars"}
    side: Literal["request", "response"] = (
        "response"
        if (status_min is not None or status_max is not None)
        else "request"
    )
    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}
    try:
        rule = engine.add(
            kind="comment",
            side=side,
            host=host,
            path_pattern=path_pattern,
            method=method,
            payload={"text": text},
            ttl_seconds=ttl_seconds,
            status_min=status_min,
            status_max=status_max,
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
    dry_run: bool = False,
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
            dry_run=dry_run,
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


# ---------------------------------------------------------------- dry_run_log


@mcp.tool()
def dry_run_log(
    rule_id: str | None = None, limit: int = 64
) -> dict[str, Any]:
    """Inspect dry-run match history.

    When a rule has ``dry_run=True``, the addons hook records every
    match instead of mutating the message. Use this tool to see which
    captures would have been touched — newest first — before flipping
    the rule to live.

    Pass ``rule_id`` to fetch one rule's log; omit it for a summary
    of how many entries each rule has.

    Returns ``{rule_id, entries[ts, uid, host, path, method, side]}``
    when a ``rule_id`` is given, or ``{by_rule: {rule_id: count}}``
    for the summary form.
    """
    daemon = get_daemon()
    if daemon.dry_run_log is None:
        return {"error": "dry-run log not available"}
    if rule_id is None:
        return {"by_rule": daemon.dry_run_log.fetch_all()}
    if not isinstance(limit, int) or limit <= 0 or limit > 256:
        return {"error": "limit must be int in (0, 256]"}
    entries = daemon.dry_run_log.fetch(rule_id, limit=limit)
    return {
        "rule_id": rule_id,
        "entries": [
            {
                "ts": e.ts, "uid": e.uid, "host": e.host,
                "path": e.path, "method": e.method, "side": e.side,
            }
            for e in entries
        ],
    }


@mcp.tool()
def clear_dry_run_log(rule_id: str | None = None) -> dict[str, int]:
    """Clear the dry-run log for a single rule, or for all rules if
    ``rule_id`` is omitted. Returns ``{cleared: int}`` (entries dropped).
    """
    daemon = get_daemon()
    if daemon.dry_run_log is None:
        return {"cleared": 0}
    return {"cleared": daemon.dry_run_log.clear(rule_id)}


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
    dry_run: bool = False,
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
            dry_run=dry_run,
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
    dry_run: bool = False,
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
            dry_run=dry_run,
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
    dry_run: bool = False,
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
            dry_run=dry_run,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"rule_id": rule.id, "expires_at": rule.expires_ts}


# ---------------------------------------------------------------- patch_body_field
# Tier 3 — surgical JSON field rewrite.

# Cap on combined regex pattern + replacement length. Defends a tiny
# bit against pathological catastrophic-backtracking patterns; not a
# substitute for caller discipline.
_REGEX_INPUT_MAX_BYTES: int = 4 * 1024


def _validate_field_path(path: Any) -> str | None:
    """Returns an error message if the path is unusable, else None."""
    if not isinstance(path, str) or not path:
        return "field_path must be a non-empty string"
    if len(path) > 256:
        return "field_path must be ≤ 256 chars"
    if path.startswith(".") or path.endswith("."):
        return "field_path cannot start or end with '.'"
    if ".." in path:
        return "field_path cannot contain empty components ('..')"
    return None


def _validate_patch_value(value: Any) -> str | None:
    """JSON-serializable + size cap. ``None`` is legal (writes JSON null).
    """
    try:
        encoded = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        return f"value not JSON-serializable: {e}"
    try:
        size = len(encoded.encode("utf-8"))
    except UnicodeEncodeError as e:
        return f"value not UTF-8 encodable: {e}"
    if size > BODY_MAX_BYTES:
        return f"value exceeds BODY_MAX_BYTES={BODY_MAX_BYTES}"
    return None


@mcp.tool()
def patch_body_field(
    field_path: str,
    value: Any,
    host: str | None = None,
    path_pattern: str | None = None,
    method: str | None = None,
    side: Literal["request", "response"] = "request",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Rewrite a single JSON field inside the body, leaving everything
    else intact.

    Walking semantics for ``field_path`` (dotted):

    * dict keys: ``data.user.email``
    * list indexes (integer-only components): ``items.0.price``
    * intermediate dict keys are auto-created when missing
    * intermediate list indexes are NOT auto-extended (would corrupt)

    ``value`` can be any JSON-encodable value (``str`` / ``int`` /
    ``float`` / ``bool`` / ``None`` / ``dict`` / ``list``). Use
    ``None`` to set a field to JSON ``null``.

    Body must be text JSON. Non-JSON / binary bodies silently no-op
    (the rule simply won't match — its hit count stays at zero).

    **This rewrites real traffic.** Pair with a tight ``host`` /
    ``path_pattern`` so this doesn't touch unrelated requests.
    Default TTL 300s, max 3600s.

    Returns ``{rule_id, expires_at}`` or ``{error}``.
    """
    err = _validate_field_path(field_path)
    if err is not None:
        return {"error": err}
    err = _validate_patch_value(value)
    if err is not None:
        return {"error": err}

    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}
    try:
        rule = engine.add(
            kind="patch_field",
            side=side,
            host=host,
            path_pattern=path_pattern,
            method=method,
            payload={"field_path": field_path, "value": value},
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"rule_id": rule.id, "expires_at": rule.expires_ts}


# ---------------------------------------------------------------- replace_body_regex
# Tier 3 — text-level body rewrite via Python ``re.sub``.


_VALID_REGEX_FLAGS: dict[str, int] = {
    "i": __import__("re").IGNORECASE,
    "m": __import__("re").MULTILINE,
    "s": __import__("re").DOTALL,
    "x": __import__("re").VERBOSE,
}


def _validate_regex_inputs(
    pattern: str, replacement: str, count: int, flags: list[str] | None
) -> tuple[int, str | None]:
    """Return ``(flags_int, error)``. Pre-compiles to catch bad regex
    on the daemon side before it reaches addons."""
    if not isinstance(pattern, str) or not pattern:
        return 0, "pattern must be a non-empty string"
    if not isinstance(replacement, str):
        return 0, "replacement must be a string"
    if len(pattern.encode("utf-8")) + len(replacement.encode("utf-8")) > _REGEX_INPUT_MAX_BYTES:
        return 0, (
            f"pattern + replacement exceeds {_REGEX_INPUT_MAX_BYTES} bytes"
        )
    if not isinstance(count, int) or count < 0:
        return 0, f"count must be a non-negative int, got {count!r}"
    flags_int = 0
    for f in flags or []:
        if not isinstance(f, str) or f.lower() not in _VALID_REGEX_FLAGS:
            return 0, (
                f"unknown flag {f!r}; valid: "
                f"{sorted(_VALID_REGEX_FLAGS.keys())}"
            )
        flags_int |= _VALID_REGEX_FLAGS[f.lower()]
    import re as _re
    try:
        _re.compile(pattern, flags_int)
    except _re.error as e:
        return 0, f"pattern failed to compile: {e}"
    return flags_int, None


@mcp.tool()
def replace_body_regex(
    pattern: str,
    replacement: str,
    host: str | None = None,
    path_pattern: str | None = None,
    method: str | None = None,
    side: Literal["request", "response"] = "request",
    count: int = 0,
    flags: list[str] | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run ``re.sub(pattern, replacement, body)`` on matching traffic.

    Operates on the body's text payload — non-text bodies silently
    no-op. ``count=0`` means "replace all" (``re.sub`` semantics);
    pass ``count=1`` to only rewrite the first match.

    ``flags`` is a list of single-letter flags:
    ``"i"`` (case-insensitive), ``"m"`` (multiline), ``"s"`` (dotall),
    ``"x"`` (verbose). Pass ``None`` for no flags.

    Pattern syntax is Python ``re`` — back-references in
    ``replacement`` are ``\\1`` / ``\\g<name>`` etc. The pattern is
    pre-compiled at install time so an invalid regex returns
    ``{error}`` immediately rather than silently failing per request.

    Caveat: ``re.sub`` has no built-in timeout. Avoid catastrophic
    backtracking patterns like ``(a+)+$`` on adversarial bodies.

    Pair with ``host`` / ``path_pattern`` so the rewrite scope is
    narrow. Default TTL 300s.

    Returns ``{rule_id, expires_at}`` or ``{error}``.
    """
    flags_int, err = _validate_regex_inputs(pattern, replacement, count, flags)
    if err is not None:
        return {"error": err}

    engine = _engine_or_error()
    if engine is None:
        return {"error": "rule engine not available — daemon not fully started"}
    try:
        rule = engine.add(
            kind="regex_replace",
            side=side,
            host=host,
            path_pattern=path_pattern,
            method=method,
            payload={
                "pattern": pattern,
                "replacement": replacement,
                "count": count,
                "flags": flags_int,
            },
            ttl_seconds=ttl_seconds,
            dry_run=dry_run,
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
    dry_run: bool = False,
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

    # Pre-validate everything ``engine.add`` could fail on. Rolling
    # back via ``remove`` after the second ``add`` rejects works for
    # in-process failures, but a daemon crash *between* "extract was
    # persisted" and "inject was rejected → remove called" would
    # leave an orphaned extract rule that silently stores tokens for
    # nobody. Catching the regex compile up here closes that window.
    for label, pat in (
        ("source_path_pattern", source_path_pattern),
        ("target_path_pattern", target_path_pattern),
    ):
        if pat is not None:
            try:
                re.compile(pat)
            except re.error as e:
                return {"error": f"invalid {label}: {e}"}
    if not isinstance(ttl_seconds, int) or ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        return {
            "error": (
                f"ttl_seconds must be int in (0, {MAX_TTL_SECONDS}], "
                f"got {ttl_seconds!r}"
            )
        }

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
            dry_run=dry_run,
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
            dry_run=dry_run,
        )
    except ValueError as e:
        # Last-resort rollback. Pre-validation should have caught
        # everything that can fail here in practice; this branch is
        # for genuinely unexpected failures.
        engine.remove(extract_rule.id)
        return {"error": f"inject rule rejected: {e}"}

    return {
        "relay_name": relay_name,
        "extract_rule_id": extract_rule.id,
        "inject_rule_id": inject_rule.id,
        "expires_at": extract_rule.expires_ts,
    }


__all__: list[str] = []  # tools register themselves via @mcp.tool
