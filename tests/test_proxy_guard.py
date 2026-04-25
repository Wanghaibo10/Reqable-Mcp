"""Tests for proxy_guard.

Coverage targets:
  - scrub_env removes every known proxy var and sets NO_PROXY=*
  - _parse_scutil_output handles the real macOS output format
  - SystemProxyState.points_to_loopback distinguishes Reqable vs Clash
  - assert_proxy_safe in strict mode exits on third-party proxy
"""

from __future__ import annotations

import os

import pytest

from reqable_mcp import proxy_guard
from reqable_mcp.proxy_guard import (
    PROXY_ENV_VARS,
    SystemProxyState,
    _parse_scutil_output,
    assert_proxy_safe,
    scrub_env,
)


def test_scrub_env_removes_all_proxy_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in PROXY_ENV_VARS:
        monkeypatch.setenv(v, "http://example.com:1234")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    removed = scrub_env()

    assert set(removed) == set(PROXY_ENV_VARS)
    for v in PROXY_ENV_VARS:
        assert v not in os.environ
    assert os.environ["NO_PROXY"] == "*"
    assert os.environ["no_proxy"] == "*"


def test_scrub_env_idempotent_when_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for v in PROXY_ENV_VARS:
        monkeypatch.delenv(v, raising=False)

    removed = scrub_env()

    assert removed == []
    assert os.environ["NO_PROXY"] == "*"


def test_parse_scutil_no_proxy() -> None:
    sample = """<dictionary> {
  ExceptionsList : <array> {
    0 : *.local
  }
  FTPPassive : 1
  HTTPEnable : 0
  HTTPSEnable : 0
  SOCKSEnable : 0
}"""
    s = _parse_scutil_output(sample)
    assert not s.any_enabled
    assert s.points_to_loopback() is True


def test_parse_scutil_reqable_loopback() -> None:
    sample = """<dictionary> {
  HTTPEnable : 1
  HTTPPort   : 9001
  HTTPProxy  : 127.0.0.1
  HTTPSEnable : 1
  HTTPSPort  : 9001
  HTTPSProxy : 127.0.0.1
  SOCKSEnable : 0
}"""
    s = _parse_scutil_output(sample)
    assert s.any_enabled
    assert s.http_host == "127.0.0.1"
    assert s.http_port == 9001
    assert s.https_port == 9001
    assert s.points_to_loopback() is True  # Reqable on loopback is allowed


def test_parse_scutil_clash_third_party() -> None:
    sample = """<dictionary> {
  HTTPEnable : 1
  HTTPPort   : 7890
  HTTPProxy  : 192.168.1.10
  HTTPSEnable : 1
  HTTPSPort  : 7890
  HTTPSProxy : 192.168.1.10
  SOCKSEnable : 0
}"""
    s = _parse_scutil_output(sample)
    assert s.any_enabled
    assert s.points_to_loopback() is False


def test_assert_proxy_safe_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """No proxy at all → no warning, no exit."""
    monkeypatch.setattr(
        proxy_guard,
        "detect_system_proxy",
        lambda: SystemProxyState(False, False, False),
    )
    monkeypatch.delenv("REQABLE_MCP_STRICT_PROXY", raising=False)

    state = assert_proxy_safe()
    assert state.any_enabled is False


def test_assert_proxy_safe_strict_exits_on_third_party(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        proxy_guard,
        "detect_system_proxy",
        lambda: SystemProxyState(
            http_enabled=True,
            https_enabled=False,
            socks_enabled=False,
            http_host="192.168.1.10",
            http_port=7890,
        ),
    )
    monkeypatch.setenv("REQABLE_MCP_STRICT_PROXY", "1")

    with pytest.raises(SystemExit) as excinfo:
        assert_proxy_safe()

    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "non-loopback system proxy" in err
    assert "strict mode" in err


def test_assert_proxy_safe_warns_on_third_party(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default (non-strict) mode logs warning but keeps running."""
    monkeypatch.setattr(
        proxy_guard,
        "detect_system_proxy",
        lambda: SystemProxyState(
            http_enabled=True,
            https_enabled=False,
            socks_enabled=False,
            http_host="192.168.1.10",
            http_port=7890,
        ),
    )
    monkeypatch.delenv("REQABLE_MCP_STRICT_PROXY", raising=False)

    state = assert_proxy_safe()
    assert state.any_enabled
    err = capsys.readouterr().err
    assert "non-loopback system proxy" in err
