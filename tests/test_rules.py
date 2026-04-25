"""Tests for the Phase 2 rule engine."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from reqable_mcp.rules import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    RuleEngine,
)


@pytest.fixture
def engine(tmp_path: Path) -> RuleEngine:
    return RuleEngine(tmp_path / "rules.json")


# ---------------------------------------------------------------- add / list


class TestAdd:
    def test_basic_add(self, engine: RuleEngine) -> None:
        r = engine.add(
            kind="tag",
            side="request",
            host="api.example.com",
            payload={"color": "red"},
        )
        assert r.id and len(r.id) == 32  # uuid4 hex
        assert r.host == "api.example.com"
        assert r.expires_ts is not None
        assert engine.list_all() == [r]

    def test_normalizes_host_and_method(self, engine: RuleEngine) -> None:
        r = engine.add(
            kind="tag",
            side="request",
            host="API.Example.COM",
            method="post",
            payload={"color": "blue"},
        )
        assert r.host == "api.example.com"
        assert r.method == "POST"

    def test_invalid_kind_rejected(self, engine: RuleEngine) -> None:
        with pytest.raises(ValueError, match="invalid kind"):
            engine.add(kind="bogus", side="request", payload={})  # type: ignore[arg-type]

    def test_invalid_side_rejected(self, engine: RuleEngine) -> None:
        with pytest.raises(ValueError, match="invalid side"):
            engine.add(kind="tag", side="middle", payload={})  # type: ignore[arg-type]

    def test_mock_must_be_response_side(self, engine: RuleEngine) -> None:
        with pytest.raises(ValueError, match="mock.*response"):
            engine.add(kind="mock", side="request", payload={"status": 200})

    def test_block_must_be_request_side(self, engine: RuleEngine) -> None:
        with pytest.raises(ValueError, match="block.*request"):
            engine.add(kind="block", side="response", payload={})

    def test_invalid_path_pattern_rejected(self, engine: RuleEngine) -> None:
        with pytest.raises(ValueError, match="invalid path_pattern"):
            engine.add(
                kind="tag",
                side="request",
                payload={"color": "red"},
                path_pattern="(invalid",
            )

    def test_ttl_too_large_rejected(self, engine: RuleEngine) -> None:
        with pytest.raises(ValueError, match="MAX_TTL_SECONDS"):
            engine.add(
                kind="tag",
                side="request",
                payload={"color": "red"},
                ttl_seconds=MAX_TTL_SECONDS + 1,
            )

    def test_ttl_zero_rejected(self, engine: RuleEngine) -> None:
        with pytest.raises(ValueError, match="must be > 0"):
            engine.add(
                kind="tag",
                side="request",
                payload={"color": "red"},
                ttl_seconds=0,
            )

    def test_ttl_none_means_no_expiry(self, engine: RuleEngine) -> None:
        r = engine.add(
            kind="tag",
            side="request",
            payload={"color": "red"},
            ttl_seconds=None,
        )
        assert r.expires_ts is None
        assert not r.is_expired()


# ---------------------------------------------------------------- match


class TestMatch:
    def test_host_filter(self, engine: RuleEngine) -> None:
        r = engine.add(
            kind="tag", side="request", host="a.com", payload={"color": "red"}
        )
        engine.add(
            kind="tag", side="request", host="b.com", payload={"color": "blue"}
        )
        hits = engine.match_for(side="request", host="a.com", path="/x", method="GET")
        assert [h.id for h in hits] == [r.id]

    def test_path_regex(self, engine: RuleEngine) -> None:
        r = engine.add(
            kind="tag",
            side="request",
            payload={"color": "red"},
            path_pattern=r"/api/v\d+/users",
        )
        assert engine.match_for(side="request", host="x", path="/api/v2/users", method="GET") == [r]
        assert engine.match_for(side="request", host="x", path="/api/users", method="GET") == []

    def test_method_filter(self, engine: RuleEngine) -> None:
        r = engine.add(
            kind="inject_header",
            side="request",
            method="POST",
            payload={"name": "X-A", "value": "1"},
        )
        assert engine.match_for(side="request", host="x", path="/", method="POST") == [r]
        assert engine.match_for(side="request", host="x", path="/", method="GET") == []

    def test_side_filter(self, engine: RuleEngine) -> None:
        engine.add(kind="tag", side="request", payload={"color": "red"})
        engine.add(kind="tag", side="response", payload={"color": "blue"})
        assert len(engine.match_for(side="request", host="x", path="/", method="GET")) == 1
        assert len(engine.match_for(side="response", host="x", path="/", method="GET")) == 1

    def test_host_none_matches_any(self, engine: RuleEngine) -> None:
        r = engine.add(kind="tag", side="request", payload={"color": "red"})
        assert engine.match_for(side="request", host="anywhere", path="/", method="GET") == [r]

    def test_match_for_case_insensitive_host_and_method(self, engine: RuleEngine) -> None:
        engine.add(kind="tag", side="request", host="api.x.com", payload={"color": "red"})
        # Caller may pass mixed case; engine handles it.
        hits = engine.match_for(
            side="request", host="API.X.COM", path="/", method="get"
        )
        assert len(hits) == 1


# ---------------------------------------------------------------- expiry


class TestExpiry:
    def test_expired_rule_filtered_from_match(
        self, engine: RuleEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r = engine.add(
            kind="tag",
            side="request",
            host="x",
            payload={"color": "red"},
            ttl_seconds=1,
        )
        # Force time to past expiry.
        future = r.expires_ts + 1  # type: ignore[operator]
        monkeypatch.setattr(time, "time", lambda: future)
        assert engine.match_for(side="request", host="x", path="/", method="GET") == []
        # list_all also filters out expired
        assert engine.list_all() == []

    def test_reap_expired(
        self, engine: RuleEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine.add(
            kind="tag", side="request", payload={"color": "red"}, ttl_seconds=1
        )
        engine.add(
            kind="tag", side="request", payload={"color": "blue"}, ttl_seconds=600
        )
        future = time.time() + 100
        monkeypatch.setattr(time, "time", lambda: future)
        n = engine.reap_expired()
        assert n == 1

    def test_reap_no_op_when_nothing_expired(self, engine: RuleEngine) -> None:
        engine.add(kind="tag", side="request", payload={"color": "red"})
        assert engine.reap_expired() == 0


# ---------------------------------------------------------------- persistence


class TestPersistence:
    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "rules.json"
        e = RuleEngine(path)
        r = e.add(
            kind="tag",
            side="request",
            host="a.com",
            payload={"color": "red"},
        )
        assert path.exists()
        # Confirm 0600 perms
        assert oct(path.stat().st_mode)[-3:] == "600"

        # Auto-load on construction — no explicit load() needed.
        e2 = RuleEngine(path)
        assert [x.id for x in e2.list_all()] == [r.id]

    def test_autoload_default_true(self, tmp_path: Path) -> None:
        path = tmp_path / "rules.json"
        e = RuleEngine(path)
        e.add(kind="tag", side="request", payload={"color": "red"})
        # Fresh instance should pick up persisted rules without load().
        e2 = RuleEngine(path)
        assert len(e2.list_all()) == 1

    def test_autoload_can_be_disabled(self, tmp_path: Path) -> None:
        path = tmp_path / "rules.json"
        e = RuleEngine(path)
        e.add(kind="tag", side="request", payload={"color": "red"})
        e2 = RuleEngine(path, autoload=False)
        assert e2.list_all() == []
        e2.load()
        assert len(e2.list_all()) == 1

    def test_load_drops_corrupt_field_types(self, tmp_path: Path) -> None:
        # rules.json with one good rule and one with bad ttl type.
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "id": "good",
                            "kind": "tag",
                            "side": "request",
                            "host": None,
                            "path_pattern": None,
                            "method": None,
                            "payload": {"color": "red"},
                            "created_ts": 1000.0,
                            "expires_ts": 9999999999.0,
                            "hits": 0,
                        },
                        {
                            "id": "bad",
                            "kind": "tag",
                            "side": "request",
                            "host": None,
                            "path_pattern": None,
                            "method": None,
                            "payload": {"color": "red"},
                            "created_ts": "not a number",  # wrong type
                            "expires_ts": None,
                            "hits": 0,
                        },
                    ]
                }
            )
        )
        e = RuleEngine(path)
        ids = [r.id for r in e.list_all()]
        assert ids == ["good"]

    def test_load_ignores_unknown_fields(self, tmp_path: Path) -> None:
        # Forward-compat: a rule written by a future version that adds
        # a new field should still be parseable.
        path = tmp_path / "rules.json"
        path.write_text(
            json.dumps(
                {
                    "rules": [
                        {
                            "id": "f",
                            "kind": "tag",
                            "side": "request",
                            "host": None,
                            "path_pattern": None,
                            "method": None,
                            "payload": {"color": "red"},
                            "created_ts": 1000.0,
                            "expires_ts": 9999999999.0,
                            "hits": 0,
                            "future_field_we_dont_know_about": "shrug",
                        }
                    ]
                }
            )
        )
        e = RuleEngine(path)
        assert len(e.list_all()) == 1

    def test_load_drops_already_expired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "rules.json"
        e = RuleEngine(path)
        e.add(
            kind="tag", side="request", payload={"color": "red"}, ttl_seconds=1
        )
        # Time-travel before reload so the persisted rule is "expired".
        future = time.time() + 999
        monkeypatch.setattr(time, "time", lambda: future)
        e2 = RuleEngine(path)
        e2.load()
        assert e2.list_all() == []

    def test_load_missing_file_no_op(self, tmp_path: Path) -> None:
        e = RuleEngine(tmp_path / "nope.json")
        e.load()  # should not raise
        assert e.list_all() == []

    def test_load_corrupt_file_starts_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "rules.json"
        path.write_text("{ this is not json")
        e = RuleEngine(path)
        e.load()
        assert e.list_all() == []

    def test_load_unexpected_shape_starts_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "rules.json"
        path.write_text(json.dumps({"unrelated": []}))
        e = RuleEngine(path)
        e.load()
        assert e.list_all() == []


# ---------------------------------------------------------------- mutation


class TestMutation:
    def test_remove_existing(self, engine: RuleEngine) -> None:
        r = engine.add(kind="tag", side="request", payload={"color": "red"})
        assert engine.remove(r.id) is True
        assert engine.list_all() == []

    def test_remove_unknown(self, engine: RuleEngine) -> None:
        assert engine.remove("does-not-exist") is False

    def test_clear(self, engine: RuleEngine) -> None:
        engine.add(kind="tag", side="request", payload={"color": "red"})
        engine.add(kind="tag", side="request", payload={"color": "blue"})
        assert engine.clear() == 2
        assert engine.list_all() == []

    def test_record_hit(self, engine: RuleEngine) -> None:
        r = engine.add(kind="tag", side="request", payload={"color": "red"})
        assert engine.record_hit(r.id) is True
        assert engine.record_hit(r.id) is True
        assert engine.list_all()[0].hits == 2

    def test_record_hit_unknown(self, engine: RuleEngine) -> None:
        assert engine.record_hit("nonexistent") is False


# ---------------------------------------------------------------- payload shape


class TestAddonPayload:
    def test_addon_payload_includes_id_kind_and_payload(self, engine: RuleEngine) -> None:
        r = engine.add(
            kind="inject_header",
            side="request",
            payload={"name": "X-A", "value": "1"},
        )
        ap = r.to_addon_payload()
        assert ap == {"id": r.id, "kind": "inject_header", "name": "X-A", "value": "1"}


# ---------------------------------------------------------------- stats


class TestStats:
    def test_stats_counts(self, engine: RuleEngine) -> None:
        engine.add(kind="tag", side="request", payload={"color": "red"})
        engine.add(kind="tag", side="response", payload={"color": "blue"})
        engine.add(
            kind="inject_header",
            side="request",
            payload={"name": "X", "value": "y"},
        )
        s = engine.stats()
        assert s["active"] == 3
        assert s["by_kind"]["tag"] == 2
        assert s["by_kind"]["inject_header"] == 1
        assert "by_kind.tag" not in s, "stats should nest by_kind, not flatten"


# ---------------------------------------------------------------- defaults


class TestDefaults:
    def test_default_ttl_set(self, engine: RuleEngine) -> None:
        before = time.time()
        r = engine.add(kind="tag", side="request", payload={"color": "red"})
        after = time.time()
        assert r.expires_ts is not None
        # Default TTL is DEFAULT_TTL_SECONDS, ±1s for clock noise.
        assert before + DEFAULT_TTL_SECONDS - 1 <= r.expires_ts <= after + DEFAULT_TTL_SECONDS + 1
