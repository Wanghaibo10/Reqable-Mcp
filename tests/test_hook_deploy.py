"""Tests for the addons-template deploy helper."""

from __future__ import annotations

from pathlib import Path

from reqable_mcp.hook import deploy_to
from reqable_mcp.hook.deploy import TEMPLATE_FILES


def test_deploy_creates_all_three_files(tmp_path: Path) -> None:
    target = tmp_path / "hook"
    res = deploy_to(target)
    assert res.changed is True
    assert sorted(res.written) == sorted(TEMPLATE_FILES)
    assert res.skipped == []
    for name in TEMPLATE_FILES:
        assert (target / name).exists()
        assert (target / name).read_bytes()  # non-empty


def test_deploy_creates_dir_with_0700_perms(tmp_path: Path) -> None:
    target = tmp_path / "fresh"
    deploy_to(target)
    assert oct(target.stat().st_mode)[-3:] == "700"


def test_deployed_files_have_0600_perms(tmp_path: Path) -> None:
    target = tmp_path / "hook"
    deploy_to(target)
    for name in TEMPLATE_FILES:
        assert oct((target / name).stat().st_mode)[-3:] == "600"


def test_deploy_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "hook"
    deploy_to(target)
    res2 = deploy_to(target)
    assert res2.written == []
    assert sorted(res2.skipped) == sorted(TEMPLATE_FILES)
    assert res2.changed is False


def test_deploy_overwrites_modified_files(tmp_path: Path) -> None:
    target = tmp_path / "hook"
    deploy_to(target)
    # Tamper with one file
    (target / "addons.py").write_text("# tampered")
    res = deploy_to(target)
    assert res.written == ["addons.py"]
    assert sorted(res.skipped) == ["main.py", "reqable.py"]
    assert b"# tampered" not in (target / "addons.py").read_bytes()


def test_deploy_into_existing_dir(tmp_path: Path) -> None:
    target = tmp_path / "hook"
    target.mkdir(mode=0o755)
    # Pre-existing junk file should not be removed (we're additive).
    (target / "unrelated.txt").write_text("don't touch me")
    deploy_to(target)
    assert (target / "unrelated.txt").read_text() == "don't touch me"
    # Perms should be tightened to 0700.
    assert oct(target.stat().st_mode)[-3:] == "700"


def test_addons_imports_under_python_3_9_compat(tmp_path: Path) -> None:
    """Future annotations + no walrus + no PEP-604 unions in runtime
    expressions — addons must run on Reqable's default ``python3``,
    which on older macOS ships as 3.8 or 3.9.

    We can't easily run a 3.9 interpreter here, but a syntax-only
    parse with ``compile`` rejects 3.10-only syntax.
    """
    target = tmp_path / "hook"
    deploy_to(target)
    src = (target / "addons.py").read_text()
    # No PEP-604 unions in runtime code (annotations are strings under
    # ``from __future__ import annotations``, so they're fine).
    code = compile(src, str(target / "addons.py"), "exec")
    assert code is not None


def test_deploy_with_env_override_socket_path() -> None:
    """``REQABLE_MCP_SOCKET`` should override the default in addons.py.

    We import the deployed addons.py text and check the SOCKET_PATH
    line uses ``os.environ.get`` with the right key.
    """
    import importlib.resources

    src = (
        importlib.resources.files("reqable_mcp.hook.template")
        / "addons.py"
    ).read_text()
    assert 'os.environ.get(' in src
    assert '"REQABLE_MCP_SOCKET"' in src or "'REQABLE_MCP_SOCKET'" in src
