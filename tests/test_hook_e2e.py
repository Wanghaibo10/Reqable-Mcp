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


def _run_hook_raw(
    hook_dir: Path, side: str, sample: dict, *, socket_path: Path
) -> subprocess.CompletedProcess[str]:
    """Like ``_run_hook`` but doesn't enforce success — used to assert
    on ``block`` aborts where main.py is expected to exit non-zero."""
    bin_path = hook_dir / f"{side}.bin"
    bin_path.write_text(json.dumps(sample))
    cb_path = hook_dir / f"{side}.bin.cb"
    if cb_path.exists():
        cb_path.unlink()

    env = os.environ.copy()
    env["REQABLE_MCP_SOCKET"] = str(socket_path)
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.run(
        ["python3", str(hook_dir / "main.py"), side, str(bin_path)],
        capture_output=True, text=True, env=env, cwd=str(hook_dir), timeout=10,
    )


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


def test_replace_body_request_string(hook_setup) -> None:
    d = hook_setup["daemon"]
    d.rule_engine.add(
        kind="replace_body", side="request",
        host="api.example.com",
        payload={"body": "fully replaced"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    body = cb["request"]["body"]
    assert body["type"] == 1
    assert body["payload"]["text"] == "fully replaced"
    time.sleep(0.05)
    assert d.rule_engine.list_all()[0].hits == 1


def test_replace_body_response_dict(hook_setup) -> None:
    d = hook_setup["daemon"]
    d.rule_engine.add(
        kind="replace_body", side="response",
        host="api.example.com",
        payload={"body": {"injected": True, "n": 7}},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "response", SAMPLE_RESPONSE,
        socket_path=hook_setup["socket"],
    )
    # HttpBody.of() json.dumps the dict, so we get a text body back.
    body = cb["response"]["body"]
    assert body["type"] == 1
    decoded = json.loads(body["payload"]["text"])
    assert decoded == {"injected": True, "n": 7}


def test_mock_response_status_only(hook_setup) -> None:
    d = hook_setup["daemon"]
    d.rule_engine.add(
        kind="mock", side="response",
        host="api.example.com",
        payload={"status": 451},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "response", SAMPLE_RESPONSE,
        socket_path=hook_setup["socket"],
    )
    assert cb["response"]["code"] == 451
    # Body is unchanged because the rule didn't set one.
    assert (
        cb["response"]["body"]["payload"]["text"]
        == SAMPLE_RESPONSE["response"]["body"]["payload"]["text"]
    )


def test_block_request_aborts_with_hit_recorded(hook_setup) -> None:
    """Block kind raises in onRequest, so main.py exits non-zero and
    no cb file is written — Reqable then fails the upstream session.
    The hit must still be recorded so the user can see the rule fired.
    """
    d = hook_setup["daemon"]
    rule = d.rule_engine.add(
        kind="block", side="request",
        host="api.example.com",
        payload={},
    )
    res = _run_hook_raw(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    assert res.returncode != 0
    assert "blocked by rule" in res.stderr
    cb_path = hook_setup["hook_dir"] / "request.bin.cb"
    assert not cb_path.exists()
    # Most importantly: hit recorded before the abort.
    time.sleep(0.05)
    assert d.rule_engine.list_all()[0].id == rule.id
    assert d.rule_engine.list_all()[0].hits == 1


def test_block_request_filtered_out_does_not_fire(hook_setup) -> None:
    """A block rule scoped to a different host should let traffic
    through normally."""
    d = hook_setup["daemon"]
    d.rule_engine.add(
        kind="block", side="request",
        host="ads.other.com",
        payload={},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    # Untouched
    assert cb["request"]["headers"] == SAMPLE_REQUEST["request"]["headers"]
    assert d.rule_engine.list_all()[0].hits == 0


def test_block_short_circuits_other_request_rules(hook_setup) -> None:
    """When ``block`` and ``inject_header`` both match the same request,
    the addons template must abort without applying inject — its hit
    counter would otherwise inflate while the request goes nowhere.
    """
    d = hook_setup["daemon"]
    inject = d.rule_engine.add(
        kind="inject_header", side="request",
        host="api.example.com",
        payload={"name": "X-Should-Not-Apply", "value": "racy"},
    )
    block = d.rule_engine.add(
        kind="block", side="request",
        host="api.example.com",
        payload={},
    )
    res = _run_hook_raw(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    assert res.returncode != 0
    assert "blocked by rule" in res.stderr
    cb_path = hook_setup["hook_dir"] / "request.bin.cb"
    assert not cb_path.exists()
    time.sleep(0.05)
    rules_now = {r.id: r for r in d.rule_engine.list_all()}
    # Only the block was credited with a hit.
    assert rules_now[block.id].hits == 1
    assert rules_now[inject.id].hits == 0


def test_multiple_block_rules_all_record_hits(hook_setup) -> None:
    """If two block rules match the same request, both should be
    credited so the operator can see which scopes actually fired."""
    d = hook_setup["daemon"]
    b1 = d.rule_engine.add(
        kind="block", side="request",
        host="api.example.com",
        payload={},
    )
    b2 = d.rule_engine.add(
        kind="block", side="request",
        host="api.example.com",
        path_pattern="/v1/login",
        payload={},
    )
    res = _run_hook_raw(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    assert res.returncode != 0
    time.sleep(0.05)
    by_id = {r.id: r for r in d.rule_engine.list_all()}
    assert by_id[b1.id].hits == 1
    assert by_id[b2.id].hits == 1


def test_relay_extract_then_inject(hook_setup) -> None:
    """End-to-end: response on source host writes a token into the
    relay store; subsequent request on target host pulls it back and
    sets the configured header."""
    d = hook_setup["daemon"]

    # Build a response on the source host with a token in JSON body.
    resp_sample = {
        "context": {
            **SAMPLE_RESPONSE["context"],  # type: ignore[arg-type]
            "host": "login.example.com",
        },
        "response": {
            "request": {
                **SAMPLE_REQUEST["request"],  # type: ignore[arg-type]
                "method": "POST",
                "path": "/oauth/token",
            },
            "code": 200,
            "message": "OK",
            "protocol": "h2",
            "headers": ["content-type: application/json"],
            "body": {
                "type": 1,
                "payload": {
                    "text": '{"data":{"access_token":"sek-rit-token"}}',
                    "charset": "UTF-8",
                },
            },
            "trailers": [],
        },
    }
    d.rule_engine.add(
        kind="relay_extract", side="response",
        host="login.example.com",
        payload={
            "name": "auth",
            "source_loc": "json_body",
            "source_field": "data.access_token",
            "ttl_seconds": 60,
        },
    )
    _run_hook(
        hook_setup["hook_dir"], "response", resp_sample,
        socket_path=hook_setup["socket"],
    )
    time.sleep(0.05)
    assert d.relay_store.get("auth") == "sek-rit-token"

    # Now an outbound request to a different host: relay_inject
    # should attach the token.
    d.rule_engine.add(
        kind="relay_inject", side="request",
        host="api.example.com",
        payload={
            "name": "auth",
            "target_header": "Authorization",
            "value_prefix": "Bearer ",
        },
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    assert any(
        "Authorization: Bearer sek-rit-token" in h
        for h in cb["request"]["headers"]
    )


def test_relay_inject_no_value_yet_does_nothing(hook_setup) -> None:
    """If the relay store has no value, inject must no-op cleanly
    (don't synthesize a header from thin air)."""
    d = hook_setup["daemon"]
    d.rule_engine.add(
        kind="relay_inject", side="request",
        host="api.example.com",
        payload={"name": "auth", "target_header": "Authorization"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    # No Authorization header was added.
    assert not any(h.lower().startswith("authorization:") for h in cb["request"]["headers"])
    # And the rule didn't take a hit.
    time.sleep(0.05)
    assert d.rule_engine.list_all()[0].hits == 0


def test_relay_extract_does_not_mutate_response_body(hook_setup) -> None:
    """Critical: ``relay_extract`` must read the body without
    mutating it. Earlier versions called ``body.jsonify()`` which
    permanently changed the payload — Reqable would then re-serialize
    a different byte sequence to the client (whitespace, key order,
    Content-Length).
    """
    d = hook_setup["daemon"]
    original_text = '{"data":{"access_token":"the-token"},"junk":1}'
    sample = {
        "context": {
            **SAMPLE_RESPONSE["context"],  # type: ignore[arg-type]
            "host": "login.example.com",
        },
        "response": {
            "request": {**SAMPLE_REQUEST["request"]},  # type: ignore[arg-type]
            "code": 200, "message": "OK", "protocol": "h2",
            "headers": ["content-type: application/json"],
            "body": {"type": 1, "payload": {"text": original_text, "charset": "UTF-8"}},
            "trailers": [],
        },
    }
    d.rule_engine.add(
        kind="relay_extract", side="response",
        host="login.example.com",
        payload={
            "name": "tok",
            "source_loc": "json_body",
            "source_field": "data.access_token",
            "ttl_seconds": 60,
        },
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "response", sample,
        socket_path=hook_setup["socket"],
    )
    # Token was extracted.
    time.sleep(0.05)
    assert d.relay_store.get("tok") == "the-token"
    # And the response body is unchanged byte-for-byte.
    assert cb["response"]["body"]["type"] == 1
    assert cb["response"]["body"]["payload"]["text"] == original_text


def test_relay_extract_runs_before_replace_body(hook_setup) -> None:
    """Apply order: ``relay_extract`` must see the original body even
    when a ``replace_body`` rule on the same response exists. Without
    a deterministic order the daemon's pack-by-size sort could put
    replace_body first, masking the token from extract."""
    d = hook_setup["daemon"]
    original = '{"data":{"access_token":"original"}}'
    sample = {
        "context": {
            **SAMPLE_RESPONSE["context"],  # type: ignore[arg-type]
            "host": "login.example.com",
        },
        "response": {
            "request": {**SAMPLE_REQUEST["request"]},  # type: ignore[arg-type]
            "code": 200, "message": "OK", "protocol": "h2",
            "headers": ["content-type: application/json"],
            "body": {"type": 1, "payload": {"text": original, "charset": "UTF-8"}},
            "trailers": [],
        },
    }
    # Two rules on the same response. addons must run extract first.
    d.rule_engine.add(
        kind="replace_body", side="response",
        host="login.example.com",
        payload={"body": {"replaced": True}},
    )
    d.rule_engine.add(
        kind="relay_extract", side="response",
        host="login.example.com",
        payload={
            "name": "tok",
            "source_loc": "json_body",
            "source_field": "data.access_token",
            "ttl_seconds": 60,
        },
    )
    _run_hook(
        hook_setup["hook_dir"], "response", sample,
        socket_path=hook_setup["socket"],
    )
    time.sleep(0.05)
    # The original token is what got stored — proving extract ran
    # before replace_body clobbered the payload.
    assert d.relay_store.get("tok") == "original"


def test_patch_body_field_request(hook_setup) -> None:
    """patch_field rewrites a single JSON field in a request body."""
    d = hook_setup["daemon"]
    sample = {
        "context": {**SAMPLE_REQUEST["context"]},  # type: ignore[arg-type]
        "request": {
            **SAMPLE_REQUEST["request"],  # type: ignore[arg-type]
            "body": {
                "type": 1,
                "payload": {
                    "text": '{"u":"alice","n":1,"deep":{"k":"old"}}',
                    "charset": "UTF-8",
                },
            },
        },
    }
    d.rule_engine.add(
        kind="patch_field", side="request",
        host="api.example.com",
        payload={"field_path": "deep.k", "value": "new"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", sample,
        socket_path=hook_setup["socket"],
    )
    body_text = cb["request"]["body"]["payload"]["text"]
    body = json.loads(body_text)
    assert body == {"u": "alice", "n": 1, "deep": {"k": "new"}}
    time.sleep(0.05)
    assert d.rule_engine.list_all()[0].hits == 1


def test_patch_body_field_creates_missing_dict_keys(hook_setup) -> None:
    """Intermediate dict keys auto-create."""
    d = hook_setup["daemon"]
    sample = {
        "context": {**SAMPLE_REQUEST["context"]},  # type: ignore[arg-type]
        "request": {
            **SAMPLE_REQUEST["request"],  # type: ignore[arg-type]
            "body": {
                "type": 1,
                "payload": {"text": '{"u":"alice"}', "charset": "UTF-8"},
            },
        },
    }
    d.rule_engine.add(
        kind="patch_field", side="request",
        host="api.example.com",
        payload={"field_path": "meta.tag", "value": "vip"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", sample,
        socket_path=hook_setup["socket"],
    )
    body = json.loads(cb["request"]["body"]["payload"]["text"])
    assert body == {"u": "alice", "meta": {"tag": "vip"}}


def test_patch_body_field_list_index(hook_setup) -> None:
    """Integer-only path components index lists."""
    d = hook_setup["daemon"]
    sample = {
        "context": {**SAMPLE_REQUEST["context"]},  # type: ignore[arg-type]
        "request": {
            **SAMPLE_REQUEST["request"],  # type: ignore[arg-type]
            "body": {
                "type": 1,
                "payload": {"text": '{"items":[{"p":1},{"p":2}]}', "charset": "UTF-8"},
            },
        },
    }
    d.rule_engine.add(
        kind="patch_field", side="request",
        host="api.example.com",
        payload={"field_path": "items.0.p", "value": 99},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", sample,
        socket_path=hook_setup["socket"],
    )
    body = json.loads(cb["request"]["body"]["payload"]["text"])
    assert body["items"][0]["p"] == 99
    assert body["items"][1]["p"] == 2  # untouched


def test_patch_body_field_silent_noop_on_non_json(hook_setup) -> None:
    """Body that isn't JSON should be left alone, no hit recorded."""
    d = hook_setup["daemon"]
    sample = {
        "context": {**SAMPLE_REQUEST["context"]},  # type: ignore[arg-type]
        "request": {
            **SAMPLE_REQUEST["request"],  # type: ignore[arg-type]
            "body": {
                "type": 1,
                "payload": {"text": "plain text not json", "charset": "UTF-8"},
            },
        },
    }
    d.rule_engine.add(
        kind="patch_field", side="request",
        host="api.example.com",
        payload={"field_path": "x", "value": "y"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", sample,
        socket_path=hook_setup["socket"],
    )
    assert cb["request"]["body"]["payload"]["text"] == "plain text not json"
    time.sleep(0.05)
    assert d.rule_engine.list_all()[0].hits == 0


def test_regex_replace_body(hook_setup) -> None:
    d = hook_setup["daemon"]
    sample = {
        "context": {**SAMPLE_REQUEST["context"]},  # type: ignore[arg-type]
        "request": {
            **SAMPLE_REQUEST["request"],  # type: ignore[arg-type]
            "body": {
                "type": 1,
                "payload": {"text": "old text and old again", "charset": "UTF-8"},
            },
        },
    }
    d.rule_engine.add(
        kind="regex_replace", side="request",
        host="api.example.com",
        payload={"pattern": r"old", "replacement": "new", "count": 0, "flags": 0},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", sample,
        socket_path=hook_setup["socket"],
    )
    assert cb["request"]["body"]["payload"]["text"] == "new text and new again"
    time.sleep(0.05)
    assert d.rule_engine.list_all()[0].hits == 1


def test_regex_replace_no_match_no_hit(hook_setup) -> None:
    d = hook_setup["daemon"]
    sample = {
        "context": {**SAMPLE_REQUEST["context"]},  # type: ignore[arg-type]
        "request": {
            **SAMPLE_REQUEST["request"],  # type: ignore[arg-type]
            "body": {
                "type": 1,
                "payload": {"text": "no relevant content here", "charset": "UTF-8"},
            },
        },
    }
    d.rule_engine.add(
        kind="regex_replace", side="request",
        host="api.example.com",
        payload={"pattern": r"missing", "replacement": "x", "count": 0, "flags": 0},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", sample,
        socket_path=hook_setup["socket"],
    )
    # Body unchanged.
    assert cb["request"]["body"]["payload"]["text"] == "no relevant content here"
    time.sleep(0.05)
    # Hit NOT recorded — re.subn returned 0 substitutions.
    assert d.rule_engine.list_all()[0].hits == 0


def test_patch_field_runs_before_replace_body(hook_setup) -> None:
    """Apply order: patch_field must run before replace_body so the
    surgical edit isn't clobbered by the wholesale replacement.
    Actually with our priority, replace_body should win — this test
    documents the contract."""
    d = hook_setup["daemon"]
    sample = {
        "context": {**SAMPLE_REQUEST["context"]},  # type: ignore[arg-type]
        "request": {
            **SAMPLE_REQUEST["request"],  # type: ignore[arg-type]
            "body": {
                "type": 1,
                "payload": {"text": '{"u":"alice"}', "charset": "UTF-8"},
            },
        },
    }
    # patch_field tries to set u=bob, replace_body wholesale-replaces.
    # With priority {patch_field=2, replace_body=4}, patch runs first
    # then replace_body clobbers — final body is the replace_body output.
    d.rule_engine.add(
        kind="replace_body", side="request",
        host="api.example.com",
        payload={"body": {"final": True}},
    )
    d.rule_engine.add(
        kind="patch_field", side="request",
        host="api.example.com",
        payload={"field_path": "u", "value": "bob"},
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", sample,
        socket_path=hook_setup["socket"],
    )
    body = json.loads(cb["request"]["body"]["payload"]["text"])
    assert body == {"final": True}, (
        "replace_body wholesale-replaces — its output wins"
    )


def test_relay_extract_from_response_header(hook_setup) -> None:
    """source_loc='header' pulls a token from a response header
    (e.g. ``Set-Cookie`` or ``X-Token``)."""
    d = hook_setup["daemon"]
    sample = {
        "context": {
            **SAMPLE_RESPONSE["context"],  # type: ignore[arg-type]
            "host": "login.example.com",
        },
        "response": {
            "request": {**SAMPLE_REQUEST["request"]},  # type: ignore[arg-type]
            "code": 200,
            "message": "OK",
            "protocol": "h2",
            "headers": ["content-type: text/plain", "x-csrf-token: abcdef"],
            "body": {"type": 0, "payload": None},
            "trailers": [],
        },
    }
    d.rule_engine.add(
        kind="relay_extract", side="response",
        host="login.example.com",
        payload={
            "name": "csrf",
            "source_loc": "header",
            "source_field": "X-Csrf-Token",
            "ttl_seconds": 60,
        },
    )
    _run_hook(
        hook_setup["hook_dir"], "response", sample,
        socket_path=hook_setup["socket"],
    )
    time.sleep(0.05)
    assert d.relay_store.get("csrf") == "abcdef"


def test_dry_run_inject_header_does_not_apply(hook_setup) -> None:
    """A dry-run inject_header rule MUST NOT actually add the header,
    but it should record a dry_run log entry and credit the hit."""
    d = hook_setup["daemon"]
    rule = d.rule_engine.add(
        kind="inject_header", side="request",
        host="api.example.com",
        payload={"name": "X-Should-Not-Apply", "value": "nope"},
        dry_run=True,
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    # Header was NOT added.
    assert not any(
        h.startswith("X-Should-Not-Apply") for h in cb["request"]["headers"]
    )
    time.sleep(0.05)
    # But the rule was credited with a hit.
    assert d.rule_engine.list_all()[0].hits == 1
    # And the dry-run log holds an entry.
    assert d.dry_run_log is not None
    entries = d.dry_run_log.fetch(rule.id)
    assert len(entries) == 1
    assert entries[0].host == "api.example.com"
    assert entries[0].path == "/v1/login"
    assert entries[0].method == "POST"
    assert entries[0].side == "request"


def test_dry_run_block_does_not_abort(hook_setup) -> None:
    """A dry-run block rule must NOT raise — traffic flows through,
    but the daemon records the would-be abort."""
    d = hook_setup["daemon"]
    rule = d.rule_engine.add(
        kind="block", side="request",
        host="api.example.com",
        payload={},
        dry_run=True,
    )
    res = _run_hook_raw(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    # Process exited successfully — block was suppressed.
    assert res.returncode == 0
    cb_path = hook_setup["hook_dir"] / "request.bin.cb"
    assert cb_path.exists()
    time.sleep(0.05)
    assert d.rule_engine.list_all()[0].hits == 1
    assert d.dry_run_log is not None
    assert len(d.dry_run_log.fetch(rule.id)) == 1


def test_dry_run_replace_body_does_not_mutate(hook_setup) -> None:
    """A dry-run replace_body must leave the body byte-identical."""
    d = hook_setup["daemon"]
    sample = {
        "context": {**SAMPLE_REQUEST["context"]},  # type: ignore[arg-type]
        "request": {
            **SAMPLE_REQUEST["request"],  # type: ignore[arg-type]
            "body": {
                "type": 1,
                "payload": {"text": "ORIGINAL", "charset": "UTF-8"},
            },
        },
    }
    d.rule_engine.add(
        kind="replace_body", side="request",
        host="api.example.com",
        payload={"body": "REPLACED"},
        dry_run=True,
    )
    cb = _run_hook(
        hook_setup["hook_dir"], "request", sample,
        socket_path=hook_setup["socket"],
    )
    # Body unchanged.
    assert cb["request"]["body"]["payload"]["text"] == "ORIGINAL"
    time.sleep(0.05)
    assert d.dry_run_log is not None
    assert d.dry_run_log.stats()["total_entries"] == 1


def test_dry_run_block_coexists_with_real_block(hook_setup) -> None:
    """If both a real block and a dry-run block match, the real one
    still aborts, but the dry-run one is also credited with a hit."""
    d = hook_setup["daemon"]
    real = d.rule_engine.add(
        kind="block", side="request",
        host="api.example.com",
        payload={},
    )
    dry = d.rule_engine.add(
        kind="block", side="request",
        host="api.example.com", path_pattern="/v1",
        payload={},
        dry_run=True,
    )
    res = _run_hook_raw(
        hook_setup["hook_dir"], "request", SAMPLE_REQUEST,
        socket_path=hook_setup["socket"],
    )
    assert res.returncode != 0
    time.sleep(0.05)
    by_id = {r.id: r for r in d.rule_engine.list_all()}
    assert by_id[real.id].hits == 1
    assert by_id[dry.id].hits == 1
    assert d.dry_run_log is not None
    assert len(d.dry_run_log.fetch(dry.id)) == 1


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
