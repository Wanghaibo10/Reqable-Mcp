"""Tests for the install/uninstall logic.

These exercise the file-write paths against a *fixture* Reqable
support directory, not the user's real one. We never touch
``~/Library/Application Support/com.reqable.macosx``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reqable_mcp.hook.install import (
    OUR_SCRIPT_ID,
    apply_plan,
    build_script_entry,
    build_script_environment,
    find_latest_backup,
    make_plan,
    restore_capture_config,
    uninstall_hook,
)
from reqable_mcp.paths import resolve


def _make_fake_reqable(tmp: Path) -> Path:
    """Create a minimal capture_config + script_environment under tmp.

    Mirrors the on-disk shape of a real Reqable install.
    """
    support = tmp / "support"
    (support / "config").mkdir(parents=True)
    (support / "box").mkdir()
    # data.mdb just needs to exist for assert_reqable_present()
    (support / "box" / "data.mdb").write_bytes(b"\x00" * 4096)

    capture_config = {
        "proxyPort": 9000,
        "scriptConfig": {"scripts": [], "isEnabled": False, "execHome": ""},
        "rewriteConfig": {"rewrites": [], "isEnabled": False},
    }
    (support / "config" / "capture_config").write_text(json.dumps(capture_config))

    script_env = {"executor": "python3", "version": "3.9.6", "home": ""}
    (support / "config" / "script_environment").write_text(json.dumps(script_env))

    return support


@pytest.fixture
def fake_setup(tmp_path: Path):
    support = _make_fake_reqable(tmp_path)
    paths = resolve(reqable_support=support, our_data=tmp_path / "ours")
    return {"paths": paths, "support": support, "tmp": tmp_path}


# ---------------------------------------------------------------- builders


class TestBuilders:
    def test_script_entry_minimum_fields(self) -> None:
        e = build_script_entry(hook_dir=Path("/x/y"))
        assert e["id"] == OUR_SCRIPT_ID
        assert e["isEnabled"] is True
        assert e["path"] == "/x/y"
        # Empty method = match all
        assert e["method"] == ""

    def test_script_environment_uses_current_python(self) -> None:
        env = build_script_environment(executor=Path("/usr/bin/python3"))
        assert env["executor"] == "/usr/bin/python3"
        # version comes from sys.version_info — must look like X.Y.Z
        parts = env["version"].split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts)


# ---------------------------------------------------------------- plan


class TestPlan:
    def test_make_plan_does_not_touch_disk(self, fake_setup) -> None:
        before = fake_setup["support"] / "config" / "capture_config"
        original = before.read_text()
        make_plan(paths=fake_setup["paths"])
        # capture_config untouched, no backups written
        assert before.read_text() == original
        # backup_dir might exist (we ensure_our_dirs), but no .bak files
        bd = fake_setup["paths"].our_backup_dir
        if bd.exists():
            assert list(bd.glob("*.bak")) == []

    def test_describe_mentions_key_fields(self, fake_setup) -> None:
        plan = make_plan(paths=fake_setup["paths"])
        d = plan.describe()
        assert "hook dir" in d
        assert "capture_config" in d
        assert "scripts[]" in d
        assert OUR_SCRIPT_ID in d


# ---------------------------------------------------------------- apply


class TestApply:
    def test_apply_writes_all_three_artifacts(self, fake_setup) -> None:
        plan = make_plan(paths=fake_setup["paths"])
        apply_plan(plan, force_running=True)

        # 1. Hook dir populated
        for n in ("main.py", "reqable.py", "addons.py"):
            assert (plan.hook_dir / n).exists(), f"{n} missing"

        # 2. capture_config has our entry, isEnabled flipped on
        cap = json.loads(fake_setup["paths"].reqable_capture_config.read_text())
        sc = cap["scriptConfig"]
        assert sc["isEnabled"] is True
        assert sc["execHome"] == str(plan.hook_dir)
        ours = [s for s in sc["scripts"] if s.get("id") == OUR_SCRIPT_ID]
        assert len(ours) == 1
        assert ours[0]["path"] == str(plan.hook_dir)

        # 3. script_environment rewritten
        env = json.loads(fake_setup["paths"].reqable_script_environment.read_text())
        assert env["executor"] == str(plan.venv_python)

        # 4. Backups exist
        assert plan.backup_capture_config.exists()
        assert plan.backup_script_environment.exists()
        # Original content preserved in backup
        backup_text = plan.backup_capture_config.read_text()
        assert "scriptConfig" in backup_text

    def test_apply_preserves_other_configs(self, fake_setup) -> None:
        # Add some unrelated entries to ensure we don't clobber them.
        cap_path = fake_setup["paths"].reqable_capture_config
        cap = json.loads(cap_path.read_text())
        cap["proxyPort"] = 9999
        cap["rewriteConfig"]["rewrites"] = [{"id": "keep-me"}]
        cap_path.write_text(json.dumps(cap))

        plan = make_plan(paths=fake_setup["paths"])
        apply_plan(plan, force_running=True)

        cap_after = json.loads(cap_path.read_text())
        assert cap_after["proxyPort"] == 9999
        assert cap_after["rewriteConfig"]["rewrites"] == [{"id": "keep-me"}]

    def test_apply_replaces_existing_reqable_mcp_entry(self, fake_setup) -> None:
        # User installed an old version; new install should overwrite.
        cap_path = fake_setup["paths"].reqable_capture_config
        cap = json.loads(cap_path.read_text())
        cap["scriptConfig"]["scripts"] = [
            {"id": OUR_SCRIPT_ID, "name": "old", "path": "/old/path"},
            {"id": "user-other-script", "name": "keep"},
        ]
        cap_path.write_text(json.dumps(cap))

        plan = make_plan(paths=fake_setup["paths"])
        apply_plan(plan, force_running=True)

        cap_after = json.loads(cap_path.read_text())
        scripts = cap_after["scriptConfig"]["scripts"]
        assert len(scripts) == 2  # no duplicate
        ours = [s for s in scripts if s.get("id") == OUR_SCRIPT_ID]
        assert ours[0]["path"] == str(plan.hook_dir)  # new path
        # User's other script untouched
        other = [s for s in scripts if s.get("id") == "user-other-script"]
        assert other[0]["name"] == "keep"

    def test_apply_atomic_no_partial_state_on_error(
        self, fake_setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the script_environment write fails, capture_config should
        already be applied (we deliberately don't ship a transactional
        write across two files — the backup IS the rollback). At
        minimum the original content must be in a backup."""
        plan = make_plan(paths=fake_setup["paths"])

        # First call to _atomic_write_json (the capture_config write)
        # succeeds; the second (script_env) raises.
        from reqable_mcp.hook import install as inst_mod

        original = inst_mod._atomic_write_json
        call_count = {"n": 0}

        def flaky(path, data):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("simulated disk full")
            return original(path, data)

        monkeypatch.setattr(inst_mod, "_atomic_write_json", flaky)

        with pytest.raises(OSError, match="disk full"):
            apply_plan(plan, force_running=True)

        # Backup must still exist for recovery.
        assert plan.backup_capture_config.exists()
        assert plan.backup_script_environment.exists()


# ---------------------------------------------------------------- uninstall


class TestUninstall:
    def test_uninstall_removes_only_our_entry(self, fake_setup) -> None:
        plan = make_plan(paths=fake_setup["paths"])
        apply_plan(plan, force_running=True)

        # Now there's also a user script.
        cap_path = fake_setup["paths"].reqable_capture_config
        cap = json.loads(cap_path.read_text())
        cap["scriptConfig"]["scripts"].append({"id": "user-script", "name": "x"})
        cap_path.write_text(json.dumps(cap))

        summary = uninstall_hook(paths=fake_setup["paths"])
        assert summary["removed_script"] is True

        cap_after = json.loads(cap_path.read_text())
        scripts = cap_after["scriptConfig"]["scripts"]
        assert len(scripts) == 1
        assert scripts[0]["id"] == "user-script"
        # isEnabled left on because user has another script
        assert cap_after["scriptConfig"]["isEnabled"] is True

    def test_uninstall_disables_when_no_scripts_left(self, fake_setup) -> None:
        plan = make_plan(paths=fake_setup["paths"])
        apply_plan(plan, force_running=True)
        # Only ours present; uninstall should flip isEnabled off.
        uninstall_hook(paths=fake_setup["paths"])
        cap = json.loads(fake_setup["paths"].reqable_capture_config.read_text())
        assert cap["scriptConfig"]["scripts"] == []
        assert cap["scriptConfig"]["isEnabled"] is False
        assert cap["scriptConfig"]["execHome"] == ""

    def test_uninstall_restores_script_environment(self, fake_setup) -> None:
        # Snapshot original env BEFORE install
        env_path = fake_setup["paths"].reqable_script_environment
        original_env = env_path.read_text()
        plan = make_plan(paths=fake_setup["paths"])
        apply_plan(plan, force_running=True)
        # post-install differs
        assert env_path.read_text() != original_env

        uninstall_hook(paths=fake_setup["paths"])
        # Should be restored from backup
        assert env_path.read_text() == original_env

    def test_uninstall_no_op_when_nothing_installed(self, fake_setup) -> None:
        # Never ran apply_plan — uninstall is harmless.
        summary = uninstall_hook(paths=fake_setup["paths"])
        assert summary["removed_script"] is False


# ---------------------------------------------------------------- helpers


class TestBackupHelpers:
    def test_find_latest_backup_picks_newest(self, fake_setup) -> None:
        plan1 = make_plan(paths=fake_setup["paths"])
        apply_plan(plan1, force_running=True)
        # Force a different mtime
        import time as _t
        _t.sleep(0.01)
        plan2 = make_plan(paths=fake_setup["paths"])
        apply_plan(plan2, force_running=True)

        latest = find_latest_backup(fake_setup["paths"], "capture_config")
        assert latest is not None
        # Newest backup wins
        assert latest.stat().st_mtime >= plan1.backup_capture_config.stat().st_mtime

    def test_restore_capture_config(self, fake_setup) -> None:
        cap_path = fake_setup["paths"].reqable_capture_config
        original = cap_path.read_text()
        plan = make_plan(paths=fake_setup["paths"])
        apply_plan(plan, force_running=True)
        assert cap_path.read_text() != original

        used = restore_capture_config(paths=fake_setup["paths"])
        assert used == plan.backup_capture_config
        # Bit-exact match because shutil.copy2 preserves contents
        assert cap_path.read_text() == original


# ---------------------------------------------------------------- safety rails


class TestSafetyRails:
    def test_apply_refuses_when_reqable_running(
        self, fake_setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from reqable_mcp.hook import install as inst_mod
        monkeypatch.setattr(inst_mod, "_is_reqable_running", lambda: True)
        plan = make_plan(paths=fake_setup["paths"])
        with pytest.raises(RuntimeError, match="Reqable.app is currently running"):
            apply_plan(plan)

    def test_apply_with_force_overrides_running_check(
        self, fake_setup, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from reqable_mcp.hook import install as inst_mod
        monkeypatch.setattr(inst_mod, "_is_reqable_running", lambda: True)
        plan = make_plan(paths=fake_setup["paths"])
        # Should NOT raise
        apply_plan(plan, force_running=True)
