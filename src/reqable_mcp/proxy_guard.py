"""Proxy loop-back guard.

Strong constraint from project owner: while reqable-mcp is in use, this
process MUST NOT route any HTTP traffic through a system proxy. Reqable
itself is the system proxy when capturing — having our own daemon also
go through it would either form a loop, pollute capture data, or
deadlock if Reqable awaits our response.

Three layers of defense (see spec.md "防系统代理回环"):

L1 — `scrub_env()`: erase all proxy env vars from this process.
L2 — `detect_system_proxy()`: read macOS system proxy via `scutil --proxy`
     and `assert_proxy_safe()` warns / exits when a third-party proxy is
     active (e.g. Clash, Surge), as opposed to Reqable itself.
L3 — never `import requests / urllib3 / aiohttp / httpx` in
     daemon / addons paths. (enforced by code review + grep tests)
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass

PROXY_ENV_VARS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "FTP_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "ftp_proxy",
)


@dataclass(frozen=True)
class SystemProxyState:
    """Snapshot of macOS system proxy configuration."""

    http_enabled: bool
    https_enabled: bool
    socks_enabled: bool
    http_host: str | None = None
    http_port: int | None = None
    https_host: str | None = None
    https_port: int | None = None
    socks_host: str | None = None
    socks_port: int | None = None

    @property
    def any_enabled(self) -> bool:
        return self.http_enabled or self.https_enabled or self.socks_enabled

    def points_to_loopback(self) -> bool:
        """True if every enabled proxy points at 127.0.0.1 / localhost.

        Reqable runs as a local mitm proxy. If the only enabled proxy is
        loopback, we treat that as "Reqable being itself" and don't warn.
        """
        if not self.any_enabled:
            return True
        hosts = [
            self.http_host if self.http_enabled else None,
            self.https_host if self.https_enabled else None,
            self.socks_host if self.socks_enabled else None,
        ]
        for h in hosts:
            if h is None:
                continue
            if h not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True


def scrub_env() -> list[str]:
    """L1: strip every proxy env var from this process.

    Returns the list of vars that were removed (for logging / tests).
    Sets ``NO_PROXY=*`` so any later HTTP client built from env still
    bypasses any proxy.
    """
    removed: list[str] = []
    for var in PROXY_ENV_VARS:
        if var in os.environ:
            removed.append(var)
            del os.environ[var]
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    return removed


def detect_system_proxy() -> SystemProxyState:
    """L2: read current macOS system proxy via ``scutil --proxy``.

    Falls back to a "no proxy" state on any error (better to silently
    proceed than to crash startup over a missing diagnostic).
    """
    try:
        result = subprocess.run(
            ["scutil", "--proxy"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if result.returncode != 0:
            return SystemProxyState(False, False, False)
        return _parse_scutil_output(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return SystemProxyState(False, False, False)


def _parse_scutil_output(text: str) -> SystemProxyState:
    """Parse the output of ``scutil --proxy``.

    Output looks like::

        <dictionary> {
          HTTPEnable : 1
          HTTPPort   : 7890
          HTTPProxy  : 127.0.0.1
          HTTPSEnable : 1
          ...
        }
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if " : " not in line:
            continue
        k, _, v = line.partition(" : ")
        fields[k.strip()] = v.strip()

    def flag(name: str) -> bool:
        return fields.get(name, "0") == "1"

    def port(name: str) -> int | None:
        v = fields.get(name)
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    return SystemProxyState(
        http_enabled=flag("HTTPEnable"),
        https_enabled=flag("HTTPSEnable"),
        socks_enabled=flag("SOCKSEnable"),
        http_host=fields.get("HTTPProxy") or None,
        http_port=port("HTTPPort"),
        https_host=fields.get("HTTPSProxy") or None,
        https_port=port("HTTPSPort"),
        socks_host=fields.get("SOCKSProxy") or None,
        socks_port=port("SOCKSPort"),
    )


def assert_proxy_safe(*, strict: bool | None = None) -> SystemProxyState:
    """Combine L1 + L2.

    1. Always call ``scrub_env()`` — remove proxy env vars unconditionally.
    2. Read system proxy state. If a non-loopback third-party proxy is
       active, warn on stderr (or exit if strict).

    ``strict`` defaults to ``True`` when env ``REQABLE_MCP_STRICT_PROXY=1``.
    Returns the detected proxy state for callers that want to log it.
    """
    if strict is None:
        strict = os.environ.get("REQABLE_MCP_STRICT_PROXY") == "1"

    scrub_env()
    state = detect_system_proxy()

    if state.any_enabled and not state.points_to_loopback():
        msg = (
            "[reqable-mcp] WARNING: a non-loopback system proxy is active "
            f"(http={state.http_host}:{state.http_port}, "
            f"https={state.https_host}:{state.https_port}, "
            f"socks={state.socks_host}:{state.socks_port}). "
            "Reqable's MCP integration assumes only Reqable itself acts as "
            "the system proxy. A second proxy layer (Clash/Surge/etc.) can "
            "cause request loops or polluted capture data. Disable it before "
            "relying on reqable-mcp output.\n"
        )
        sys.stderr.write(msg)
        if strict:
            sys.stderr.write("[reqable-mcp] strict mode: exiting.\n")
            sys.exit(2)

    return state
