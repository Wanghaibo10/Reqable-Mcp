"""End-to-end: fork main.py the way Reqable does, verify addons
talks to a live daemon and produces the right ``.cb`` file.

This is the *seam* test for M11+M12. It catches:
  * any Python-version incompatibility in addons.py
  * IPC schema mismatch between addons and the daemon handler
  * rule-application logic in addons (header injection, highlight,
    block, mock)

We use ``/tmp`` for paths because Unix-socket paths cap at 104 bytes
on macOS.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from reqable_mcp.daemon import Daemon, DaemonConfig
from reqable_mcp.hook import deploy_to
from reqable_mcp.paths import resolve

# Sample request payload matching Reqable 3.0's wire format. Inspired
# by an actual ``request.bin`` captured under
# ``~/Library/.../scripts/exec/{uuid}/``.
SAMPLE_REQUEST = {
    "context": {
        "url": "https://api.example.com/v1/login",
        "scheme": "https",
        "host": "api.example.com",
        "port": 443,
        "cid": 12345,
        "ctime": 1700000000000,
        "sid": 1,
        "stime": 1700000000000,
        "shared": None,
        "env": {},
        "app": {"name": "TestApp"},
    },
    "request": {
        "method": "POST",
        "path": "/v1/login",
        "protocol": "h2",
        "headers": ["host: api.example.com", "content-type: application/json"],
        "body": {"type": 1, "payload": {"text": '{"u":"x"}', "charset": "UTF-8"}},
        "trailers": [],
    },
}

SAMPLE_RESPONSE = {
    "context": SAMPLE_REQUEST["context"],
    "response": {
        "request": SAMPLE_REQUEST["request"],
        "code": 200,
        "message": "OK",
        "protocol": "h2",
        "headers": ["content-type: application/json"],
        "body": {"type": 1, "payload": {"text": '{"ok":true}', "charset": "UTF-8"}},
        "trailers": [],
    },
}


@pytest.fixture
def short_root() -> Iterator[Path]:
    p = Path("/tmp") / f"rmcp-e2e-{uuid.uuid4().hex[:8]}"
    p.mkdir(mode=0o700, exist_ok=False)
    try:
        yield p
    finally:
        shutil.rmtree(p, ignore_errors=True)


@pytest.fixture
def hook_setup(real_lmdb_required: Path, short_root: Path) -> Iterator[dict]:
    """Deploy hook + start daemon. Yields paths and the daemon."""
    hook_dir = short_root / "hook"
    deploy_to(hook_dir)

    support = real_lmdb_required.parent
    paths = resolve(reqable_support=support, our_data=short_root / "data")
    d = Daemon(paths=paths, config=DaemonConfig(strict_proxy=False))
    d.start()
    try:
        yield {"hook_dir": hook_dir, "daemon": d, "socket": paths.our_socket}
    finally:
        d.stop()


def _run_hook(hook_dir: Path, side: str, sample: dict, *, socket_path: Path) -> dict:
    """Fork main.py the same way Reqable does; return parsed cb file."""
    bin_path = hook_dir / f"{side}.bin"
    bin_path.write_text(json.dumps(sample))
    cb_path = hook_dir / f"{side}.bin.cb"
    if cb_path.exists():
        cb_path.unlink()

    env = os.environ.copy()
    env["REQABLE_MCP_SOCKET"] = str(socket_path)
    env["PYTHONUNBUFFERED"] = "1"
    res = subprocess.run(
        ["python3", str(hook_dir / "main.py"), side, str(bin_path)],
        capture_output=True, text=True, env=env, cwd=str(hook_dir), timeout=10,
    )
    assert res.returncode == 0, (
        f"hook exited non-zero (stderr: {res.stderr!r})"
    )
    if not cb_path.exists():
        # No rules matched; addons returned the message unchanged but
        # main.py still writes the cb file. If absent, that's a bug.
        pytest.fail(f"cb not written; stderr={res.stderr!r}")
    return json.loads(cb_path.read_text())


def test_no_rules_passes_through_unchanged(hook_setup) -> None:
    cb = _run_hook(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    assert cb["request"]["headers"] == SAMPLE_REQUEST["request"]["headers"]
    assert cb.get("highlight") is None
    assert cb.get("comment") is None


def test_inject_header_applied(hook_setup) -> None:
    d = hook_setup["daemon"]
    d.rule_engine.add(
        kind="inject_header", side="request",
        host="api.example.com",
        payload={"name": "X-Test-Token", "value": "abc123"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    assert any("X-Test-Token: abc123" in h for h in cb["request"]["headers"])
    # Hit was reported back to daemon
    time.sleep(0.05)
    rule = d.rule_engine.list_all()[0]
    assert rule.hits == 1


def test_tag_sets_highlight(hook_setup) -> None:
    d = hook_setup["daemon"]
    d.rule_engine.add(
        kind="tag", side="request",
        host="api.example.com",
        payload={"color": "red"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    # Highlight enum: red=1
    assert cb["highlight"] == 1


def test_comment_set_on_request(hook_setup) -> None:
    d = hook_setup["daemon"]
    d.rule_engine.add(
        kind="comment", side="request",
        host="api.example.com",
        payload={"text": "auto-flagged login"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    assert cb["comment"] == "auto-flagged login"


def test_response_side_mock_applies(hook_setup) -> None:
    d = hook_setup["daemon"]
    d.rule_engine.add(
        kind="mock", side="response",
        host="api.example.com",
        payload={"status": 503, "headers": {"X-Mocked": "true"}, "body": "down"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "response", SAMPLE_RESPONSE,
        socket_path=hook_setup["socket"],
    )
    assert cb["response"]["code"] == 503
    assert any("X-Mocked: true" in h for h in cb["response"]["headers"])


def test_host_mismatch_no_application(hook_setup) -> None:
    d = hook_setup["daemon"]
    d.rule_engine.add(
        kind="inject_header", side="request",
        host="other.example.com",  # rule for a different host
        payload={"name": "X-Should-Not-Appear", "value": "x"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    assert not any("X-Should-Not-Appear" in h for h in cb["request"]["headers"])


def test_addons_fail_open_when_daemon_unreachable(short_root: Path) -> None:
    """If the socket doesn't exist, addons must pass the request
    through unchanged — we never break user traffic."""
    hook_dir = short_root / "hook"
    deploy_to(hook_dir)

    bin_path = hook_dir / "request.bin"
    bin_path.write_text(json.dumps(SAMPLE_REQUEST))

    env = os.environ.copy()
    env["REQABLE_MCP_SOCKET"] = "/tmp/definitely-does-not-exist.sock"
    res = subprocess.run(
        ["python3", str(hook_dir / "main.py"), "request", str(bin_path)],
        capture_output=True, text=True, env=env, cwd=str(hook_dir), timeout=10,
    )
    assert res.returncode == 0
    cb = json.loads((hook_dir / "request.bin.cb").read_text())
    assert cb["request"]["headers"] == SAMPLE_REQUEST["request"]["headers"]
