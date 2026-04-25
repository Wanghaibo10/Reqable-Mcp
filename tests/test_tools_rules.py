"""Tests for Tier-2 rule MCP tools.

These wire a real Daemon (with RuleEngine) up to the module-level
``mcp`` server and call the registered tools, asserting the
RuleEngine reflects the changes. We don't bring up an IPC socket —
the addons-side path is covered by tests/test_hook_e2e.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reqable_mcp.daemon import Daemon, DaemonConfig
from reqable_mcp.mcp_server import set_daemon
from reqable_mcp.paths import resolve
from reqable_mcp.rules import BODY_MAX_BYTES, MAX_TTL_SECONDS


@pytest.fixture
def daemon(real_lmdb_required: Path, short_data_dir: Path):
    """Start a real daemon (no IPC) so RuleEngine is wired and tools work."""
    support = real_lmdb_required.parent
    paths = resolve(reqable_support=support, our_data=short_data_dir)
    d = Daemon(
        paths=paths,
        config=DaemonConfig(strict_proxy=False, enable_ipc=False),
    )
    d.start()
    set_daemon(d)
    # Touch the tool module so its decorators register against `mcp`.
    from reqable_mcp.tools import rules  # noqa: F401
    yield d
    d.stop()


# ---------------------------------------------------------------- tag_pattern


class TestTagPattern:
    def test_basic_tag(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import tag_pattern

        result = tag_pattern(host="api.example.com", color="red")
        assert "rule_id" in result
        assert "expires_at" in result
        # Engine has the rule
        assert daemon.rule_engine is not None
        rules = daemon.rule_engine.list_all()
        assert len(rules) == 1
        assert rules[0].kind == "tag"
        assert rules[0].payload == {"color": "red"}

    def test_invalid_color_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import tag_pattern

        result = tag_pattern(host="x", color="purple")
        assert "error" in result
        assert "color must be one of" in result["error"]

    def test_invalid_path_pattern_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import tag_pattern

        result = tag_pattern(host="x", path_pattern="(unclosed")
        assert "error" in result

    def test_ttl_too_large_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import tag_pattern

        result = tag_pattern(
            host="x", color="red", ttl_seconds=MAX_TTL_SECONDS + 1
        )
        assert "error" in result
        assert "MAX_TTL_SECONDS" in result["error"]

    def test_method_filter_normalized(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import tag_pattern

        tag_pattern(host="x", method="post", color="green")
        assert daemon.rule_engine is not None
        rules = daemon.rule_engine.list_all()
        assert rules[0].method == "POST"


# ---------------------------------------------------------------- comment_pattern


class TestCommentPattern:
    def test_basic_comment(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import comment_pattern

        result = comment_pattern(text="auth flow", host="api.x.com")
        assert "rule_id" in result
        assert daemon.rule_engine is not None
        rule = daemon.rule_engine.list_all()[0]
        assert rule.kind == "comment"
        assert rule.payload == {"text": "auth flow"}

    def test_empty_text_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import comment_pattern

        result = comment_pattern(text="", host="x")
        assert "error" in result

    def test_overlong_text_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import comment_pattern

        result = comment_pattern(text="x" * 600, host="x")
        assert "error" in result


# ---------------------------------------------------------------- inject_header


class TestInjectHeader:
    def test_basic_inject(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import inject_header

        result = inject_header(name="X-Foo", value="bar", host="api.x.com")
        assert "rule_id" in result
        assert daemon.rule_engine is not None
        rule = daemon.rule_engine.list_all()[0]
        assert rule.kind == "inject_header"
        assert rule.payload == {"name": "X-Foo", "value": "bar"}
        assert rule.side == "request"

    def test_response_side(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import inject_header

        inject_header(name="X-Test", value="1", side="response", host="x")
        assert daemon.rule_engine is not None
        rule = daemon.rule_engine.list_all()[0]
        assert rule.side == "response"

    def test_pseudo_header_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import inject_header

        result = inject_header(name=":authority", value="x")
        assert "error" in result
        assert "pseudo-header" in result["error"]

    def test_empty_name_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import inject_header

        result = inject_header(name="", value="x")
        assert "error" in result


# ---------------------------------------------------------------- list_rules


class TestListRules:
    def test_lists_only_active(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import (
            inject_header,
            list_rules,
            tag_pattern,
        )

        tag_pattern(host="a", color="red")
        inject_header(name="X-A", value="1", host="b")
        all_rules = list_rules()
        assert len(all_rules) == 2
        # Each rule should have the public-facing shape
        for r in all_rules:
            assert "id" in r
            assert "kind" in r
            assert "hits" in r

    def test_filter_by_kind(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import (
            inject_header,
            list_rules,
            tag_pattern,
        )

        tag_pattern(host="a", color="red")
        inject_header(name="X-A", value="1", host="b")
        only_tags = list_rules(kind="tag")
        assert len(only_tags) == 1
        assert only_tags[0]["kind"] == "tag"


# ---------------------------------------------------------------- remove / clear


class TestMutators:
    def test_remove_by_id(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import remove_rule, tag_pattern

        result = tag_pattern(host="x", color="red")
        rid = result["rule_id"]
        out = remove_rule(rid)
        assert out == {"removed": True}
        assert daemon.rule_engine is not None
        assert daemon.rule_engine.list_all() == []

    def test_remove_unknown(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import remove_rule

        out = remove_rule("does-not-exist")
        assert out == {"removed": False}

    def test_clear_all(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import (
            clear_rules,
            inject_header,
            tag_pattern,
        )

        tag_pattern(host="a", color="red")
        tag_pattern(host="b", color="blue")
        inject_header(name="X", value="y", host="c")
        out = clear_rules()
        assert out == {"cleared": 3}
        assert daemon.rule_engine is not None
        assert daemon.rule_engine.list_all() == []


# ---------------------------------------------------------------- ttl_limits


class TestTtlLimits:
    def test_ttl_limits_returns_constants(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import ttl_limits

        out = ttl_limits()
        assert out["max"] == MAX_TTL_SECONDS
        assert out["default"] > 0
        assert out["default"] <= out["max"]
        assert out["body_max_bytes"] == BODY_MAX_BYTES


# ---------------------------------------------------------------- replace_body


class TestReplaceBody:
    def test_string_body(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import replace_body

        result = replace_body(body="hello world", host="api.example.com")
        assert "rule_id" in result
        assert daemon.rule_engine is not None
        rules = daemon.rule_engine.list_all()
        assert len(rules) == 1
        assert rules[0].kind == "replace_body"
        assert rules[0].side == "request"
        assert rules[0].payload == {"body": "hello world"}

    def test_dict_body_preserved(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import replace_body

        body = {"foo": "bar", "n": 42}
        result = replace_body(body=body, host="api.example.com")
        assert "rule_id" in result
        assert daemon.rule_engine is not None
        rules = daemon.rule_engine.list_all()
        # We hand the dict through unchanged — addons.py's HttpBody.of()
        # json.dumps'es it on the other side.
        assert rules[0].payload["body"] == body

    def test_response_side(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import replace_body

        result = replace_body(body="x", host="x", side="response")
        assert "rule_id" in result
        assert daemon.rule_engine is not None
        assert daemon.rule_engine.list_all()[0].side == "response"

    def test_bytes_body_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import replace_body

        result = replace_body(body=b"binary stuff", host="x")  # type: ignore[arg-type]
        assert "error" in result
        assert "str or dict" in result["error"]

    def test_oversized_string_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import replace_body

        big = "a" * (BODY_MAX_BYTES + 1)
        result = replace_body(body=big, host="x")
        assert "error" in result
        assert "BODY_MAX_BYTES" in result["error"]

    def test_oversized_dict_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import replace_body

        big = {"k": "a" * BODY_MAX_BYTES}
        result = replace_body(body=big, host="x")
        assert "error" in result
        assert "BODY_MAX_BYTES" in result["error"]

    def test_non_serializable_dict_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import replace_body

        result = replace_body(body={"obj": object()}, host="x")  # type: ignore[arg-type]
        assert "error" in result

    def test_filters_normalized(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import replace_body

        replace_body(body="x", host="API.example.com", method="put")
        assert daemon.rule_engine is not None
        rule = daemon.rule_engine.list_all()[0]
        assert rule.host == "api.example.com"
        assert rule.method == "PUT"


# ---------------------------------------------------------------- mock_response


class TestMockResponse:
    def test_basic_status_only(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import mock_response

        result = mock_response(status=503, host="flaky.example.com")
        assert "rule_id" in result
        assert daemon.rule_engine is not None
        rule = daemon.rule_engine.list_all()[0]
        assert rule.kind == "mock"
        assert rule.side == "response"  # forced by engine
        assert rule.payload == {"status": 503}

    def test_full_payload(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import mock_response

        result = mock_response(
            status=418,
            body={"teapot": True},
            headers={"X-Mocked": "1"},
            host="x",
        )
        assert "rule_id" in result
        assert daemon.rule_engine is not None
        rule = daemon.rule_engine.list_all()[0]
        assert rule.payload == {
            "status": 418,
            "body": {"teapot": True},
            "headers": {"X-Mocked": "1"},
        }

    def test_empty_payload_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import mock_response

        result = mock_response(host="x")
        assert "error" in result
        assert "at least one" in result["error"]

    def test_status_out_of_range(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import mock_response

        assert "error" in mock_response(status=99, host="x")
        assert "error" in mock_response(status=601, host="x")
        assert "error" in mock_response(status="200", host="x")  # type: ignore[arg-type]

    def test_pseudo_header_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import mock_response

        result = mock_response(
            status=200, headers={":status": "200"}, host="x"
        )
        assert "error" in result
        assert "pseudo-header" in result["error"]

    def test_headers_must_be_str_str(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import mock_response

        result = mock_response(
            status=200, headers={"X-Mocked": 1}, host="x"  # type: ignore[dict-item]
        )
        assert "error" in result

    def test_oversized_body_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import mock_response

        result = mock_response(body="b" * (BODY_MAX_BYTES + 1), host="x")
        assert "error" in result
        assert "BODY_MAX_BYTES" in result["error"]

    def test_empty_headers_rejected(self, daemon: Daemon) -> None:
        """``headers={}`` would install a no-op rule that just runs
        up hit counts. Refuse to install it."""
        from reqable_mcp.tools.rules import mock_response

        result = mock_response(headers={}, host="x")
        assert "error" in result
        assert "no-op" in result["error"]
        assert daemon.rule_engine is not None
        assert daemon.rule_engine.list_all() == []

    def test_empty_header_name_rejected(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import mock_response

        result = mock_response(headers={"": "v"}, host="x")
        assert "error" in result
        assert "non-empty" in result["error"]


# ---------------------------------------------------------------- _coerce_body
# Edge cases that previously caused the tool to raise instead of
# returning a clean error.


class TestCoerceBodyEdgeCases:
    def test_lone_surrogate_string_rejected_cleanly(
        self, daemon: Daemon
    ) -> None:
        """``"\\ud800"`` is a lone high surrogate — not legal UTF-8.
        ``str.encode("utf-8")`` raises UnicodeEncodeError; we want a
        clean ``{error}`` instead of the exception bubbling out of the
        tool."""
        from reqable_mcp.tools.rules import replace_body

        result = replace_body(body="\ud800", host="x")
        assert "error" in result
        assert "UTF-8" in result["error"]

    def test_lone_surrogate_in_dict_rejected_cleanly(
        self, daemon: Daemon
    ) -> None:
        from reqable_mcp.tools.rules import replace_body

        result = replace_body(body={"k": "\ud800"}, host="x")
        assert "error" in result

    def test_exact_byte_boundary(self, daemon: Daemon) -> None:
        """A string whose UTF-8 encoding is exactly BODY_MAX_BYTES
        must be accepted; one byte over must be rejected."""
        from reqable_mcp.tools.rules import replace_body

        ok = replace_body(body="a" * BODY_MAX_BYTES, host="x")
        assert "rule_id" in ok
        too_big = replace_body(body="a" * (BODY_MAX_BYTES + 1), host="x")
        assert "error" in too_big

    def test_multibyte_utf8_counted_in_bytes(
        self, daemon: Daemon
    ) -> None:
        """Each '中' is 3 UTF-8 bytes; the cap is on bytes, not chars."""
        from reqable_mcp.tools.rules import replace_body

        # 22000 chars * 3 bytes = 66000 > 64KB → reject
        result = replace_body(body="中" * 22000, host="x")
        assert "error" in result
        assert "BODY_MAX_BYTES" in result["error"]


# ---------------------------------------------------------------- block_request


class TestBlockRequest:
    def test_basic_block(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import block_request

        result = block_request(host="ads.example.com")
        assert "rule_id" in result
        assert daemon.rule_engine is not None
        rule = daemon.rule_engine.list_all()[0]
        assert rule.kind == "block"
        assert rule.side == "request"  # forced
        assert rule.host == "ads.example.com"

    def test_unfiltered_refused(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import block_request

        result = block_request()
        assert "error" in result
        assert "filter" in result["error"]
        # Nothing was installed
        assert daemon.rule_engine is not None
        assert daemon.rule_engine.list_all() == []

    def test_path_pattern_only(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import block_request

        result = block_request(path_pattern="/track")
        assert "rule_id" in result
        assert daemon.rule_engine is not None
        assert daemon.rule_engine.list_all()[0].path_pattern == "/track"

    def test_method_only(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import block_request

        result = block_request(method="DELETE")
        assert "rule_id" in result

    def test_invalid_path_regex(self, daemon: Daemon) -> None:
        from reqable_mcp.tools.rules import block_request

        result = block_request(path_pattern="(unclosed")
        assert "error" in result

    def test_empty_string_filters_treated_as_unspecified(
        self, daemon: Daemon
    ) -> None:
        """``host=""`` / ``path_pattern=""`` / ``method=""`` slip past
        ``is None`` checks but mean the same thing — the guard must
        treat them as unspecified."""
        from reqable_mcp.tools.rules import block_request

        result = block_request(host="", path_pattern="", method="")
        assert "error" in result
        assert "non-empty filter" in result["error"]
        assert daemon.rule_engine is not None
        assert daemon.rule_engine.list_all() == []

    @pytest.mark.parametrize(
        "pattern", [".*", ".+", "^", "^.*", "^.*$", ".*?"]
    )
    def test_catchall_path_pattern_rejected(
        self, daemon: Daemon, pattern: str
    ) -> None:
        """A regex that matches every path is not a real filter — the
        rule would silently kill all traffic."""
        from reqable_mcp.tools.rules import block_request

        result = block_request(path_pattern=pattern)
        assert "error" in result
        assert "every path" in result["error"]
        assert daemon.rule_engine is not None
        assert daemon.rule_engine.list_all() == []


# ---------------------------------------------------------------- persistence


class TestPersistenceBetweenDaemons:
    def test_rules_survive_daemon_restart(
        self, real_lmdb_required: Path, short_data_dir: Path
    ) -> None:
        """Rules of every M14 + M15 kind installed via MCP tools must
        persist in rules.json and reload on next daemon start.

        Originally this only covered ``tag_pattern``; M15 added three
        new kinds and we want a regression test that all of them
        round-trip through JSON, including their kind-specific payloads.
        """
        support = real_lmdb_required.parent
        paths = resolve(reqable_support=support, our_data=short_data_dir)

        d1 = Daemon(
            paths=paths,
            config=DaemonConfig(strict_proxy=False, enable_ipc=False),
        )
        d1.start()
        set_daemon(d1)
        from reqable_mcp.tools import rules as rules_tools

        installed: dict[str, str] = {}
        installed["tag"] = rules_tools.tag_pattern(
            host="persist.test", color="green"
        )["rule_id"]
        installed["replace_body"] = rules_tools.replace_body(
            body={"persisted": True}, host="persist.test"
        )["rule_id"]
        installed["mock"] = rules_tools.mock_response(
            status=503,
            body="oops",
            headers={"X-Persisted": "1"},
            host="persist.test",
        )["rule_id"]
        installed["block"] = rules_tools.block_request(
            host="persist.test", path_pattern="/blocked"
        )["rule_id"]
        d1.stop()

        # Now bring a fresh daemon up against the same data dir.
        d2 = Daemon(
            paths=paths,
            config=DaemonConfig(strict_proxy=False, enable_ipc=False),
        )
        d2.start()
        try:
            assert d2.rule_engine is not None
            reloaded = {r.id: r for r in d2.rule_engine.list_all()}
            for kind, rid in installed.items():
                assert rid in reloaded, f"{kind} rule {rid} not reloaded"
            # Spot-check kind-specific payloads survived JSON round-trip.
            assert (
                reloaded[installed["replace_body"]].payload["body"]
                == {"persisted": True}
            )
            mock_payload = reloaded[installed["mock"]].payload
            assert mock_payload["status"] == 503
            assert mock_payload["headers"] == {"X-Persisted": "1"}
            assert reloaded[installed["block"]].kind == "block"
        finally:
            d2.stop()


# ---------------------------------------------------------------- engine missing


def test_tools_handle_missing_engine(short_data_dir: Path) -> None:
    """If the rule engine isn't wired (e.g. degraded daemon), tools
    should return clear errors rather than crash."""
    from unittest.mock import MagicMock

    fake_daemon = MagicMock()
    fake_daemon.rule_engine = None
    set_daemon(fake_daemon)

    from reqable_mcp.tools.rules import (
        block_request,
        clear_rules,
        inject_header,
        list_rules,
        mock_response,
        replace_body,
        tag_pattern,
    )

    assert "error" in tag_pattern(host="x", color="red")
    assert "error" in inject_header(name="X", value="y")
    assert list_rules() == []
    assert "error" in clear_rules()
    # Tier-3 tools too
    assert "error" in replace_body(body="x", host="x")
    assert "error" in mock_response(status=200, host="x")
    assert "error" in block_request(host="x")
