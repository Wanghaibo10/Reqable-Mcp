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
from reqable_mcp.rules import MAX_TTL_SECONDS


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


# ---------------------------------------------------------------- persistence


class TestPersistenceBetweenDaemons:
    def test_rules_survive_daemon_restart(
        self, real_lmdb_required: Path, short_data_dir: Path
    ) -> None:
        """A rule installed via the MCP tool persists in rules.json
        and is reloaded on next daemon start."""
        support = real_lmdb_required.parent
        paths = resolve(reqable_support=support, our_data=short_data_dir)

        d1 = Daemon(
            paths=paths,
            config=DaemonConfig(strict_proxy=False, enable_ipc=False),
        )
        d1.start()
        set_daemon(d1)
        from reqable_mcp.tools import rules as rules_tools

        result = rules_tools.tag_pattern(host="persist.test", color="green")
        rid = result["rule_id"]
        d1.stop()

        # Now bring a fresh daemon up against the same data dir.
        d2 = Daemon(
            paths=paths,
            config=DaemonConfig(strict_proxy=False, enable_ipc=False),
        )
        d2.start()
        try:
            assert d2.rule_engine is not None
            ids = [r.id for r in d2.rule_engine.list_all()]
            assert rid in ids
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
        clear_rules,
        inject_header,
        list_rules,
        tag_pattern,
    )

    assert "error" in tag_pattern(host="x", color="red")
    assert "error" in inject_header(name="X", value="y")
    assert list_rules() == []
    assert "error" in clear_rules()
