"""Tests for the in-memory RelayStore that backs auto_token_relay."""

from __future__ import annotations

import time

import pytest

from reqable_mcp.relay import (
    MAX_RELAY_TTL_SECONDS,
    MAX_RELAY_VALUE_BYTES,
    RelayStore,
)


def test_set_and_get() -> None:
    s = RelayStore()
    s.set("token", "abc", ttl_seconds=60)
    assert s.get("token") == "abc"


def test_get_missing_returns_none() -> None:
    s = RelayStore()
    assert s.get("nope") is None


def test_overwrite_value() -> None:
    s = RelayStore()
    s.set("k", "v1", ttl_seconds=60)
    s.set("k", "v2", ttl_seconds=60)
    assert s.get("k") == "v2"


def test_expired_value_returns_none_and_evicts() -> None:
    s = RelayStore()
    s.set("k", "v", ttl_seconds=60)
    # Force expiry by mutating the entry directly — far cleaner than
    # waiting 60s.
    with s._lock:  # type: ignore[attr-defined]
        s._values["k"].expires_ts = time.time() - 1  # type: ignore[attr-defined]
    assert s.get("k") is None
    # And the entry was lazily evicted.
    assert "k" not in s._values  # type: ignore[attr-defined]


def test_remove() -> None:
    s = RelayStore()
    s.set("k", "v", ttl_seconds=60)
    assert s.remove("k") is True
    assert s.get("k") is None
    assert s.remove("k") is False


def test_clear() -> None:
    s = RelayStore()
    s.set("a", "1", ttl_seconds=60)
    s.set("b", "2", ttl_seconds=60)
    assert s.clear() == 2
    assert s.get("a") is None


def test_reap_expired() -> None:
    s = RelayStore()
    s.set("alive", "v", ttl_seconds=60)
    s.set("dead", "v", ttl_seconds=60)
    with s._lock:  # type: ignore[attr-defined]
        s._values["dead"].expires_ts = time.time() - 1  # type: ignore[attr-defined]
    assert s.reap_expired() == 1
    assert s.get("alive") == "v"


def test_stats() -> None:
    s = RelayStore()
    s.set("a", "v", ttl_seconds=60)
    stats = s.stats()
    assert stats == {"total": 1, "active": 1}


def test_list_names_does_not_leak_values() -> None:
    s = RelayStore()
    s.set("token", "secret-value", ttl_seconds=60)
    names = s.list_names()
    assert names == ["token"]
    # Just to be explicit.
    assert "secret-value" not in str(names)


def test_empty_name_rejected() -> None:
    s = RelayStore()
    with pytest.raises(ValueError):
        s.set("", "v", ttl_seconds=60)


def test_non_string_value_rejected() -> None:
    s = RelayStore()
    with pytest.raises(TypeError):
        s.set("k", 42, ttl_seconds=60)  # type: ignore[arg-type]


def test_value_too_large_rejected() -> None:
    s = RelayStore()
    with pytest.raises(ValueError):
        s.set("k", "x" * (MAX_RELAY_VALUE_BYTES + 1), ttl_seconds=60)


def test_ttl_too_large_rejected() -> None:
    s = RelayStore()
    with pytest.raises(ValueError):
        s.set("k", "v", ttl_seconds=MAX_RELAY_TTL_SECONDS + 1)


def test_ttl_zero_or_negative_rejected() -> None:
    s = RelayStore()
    with pytest.raises(ValueError):
        s.set("k", "v", ttl_seconds=0)
    with pytest.raises(ValueError):
        s.set("k", "v", ttl_seconds=-1)


def test_cardinality_cap_rejects_new_names() -> None:
    """Once we hit MAX_RELAY_ENTRIES, distinct new names are refused
    but existing names can still be refreshed."""
    from reqable_mcp.relay import MAX_RELAY_ENTRIES

    s = RelayStore()
    for i in range(MAX_RELAY_ENTRIES):
        s.set(f"k{i}", "v", ttl_seconds=60)
    # Net-new name → reject.
    with pytest.raises(ValueError, match="store full"):
        s.set("overflow", "v", ttl_seconds=60)
    # Refreshing an existing name → still works.
    s.set("k0", "v2", ttl_seconds=60)
    assert s.get("k0") == "v2"
