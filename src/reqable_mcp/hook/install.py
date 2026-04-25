"""Install / uninstall the addons hook into Reqable's capture_config.

This is the only module in the project that *writes* into Reqable's
data directory. Everything else is read-only. Two safety rails:

1. **Backup before write**.  The current ``capture_config`` and
   ``script_environment`` are copied to ``~/.reqable-mcp/backup/``
   with a timestamp before any change. ``uninstall_hook`` either
   restores the most recent backup, or surgically removes only our
   ``reqable-mcp`` script entry while leaving the rest intact.

2. **Reqable must be quit**.  Reqable holds these JSON files in
   memory and rewrites them on its own schedule. Modifying them
   while Reqable runs invites a lost-write race. ``install_hook``
   refuses unless ``--force`` is given.

The script entry shape is *educated-guess* — Reqable doesn't ship
sample scripts to crib from. We start from a minimal entry close to
peers like ``rewriteConfig.rewrites`` (id/name/method/url/wildcard
+ enabled). If Reqable rejects it, the user can compare the
generated config against one made via the UI and update
:func:`build_script_entry`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import Paths, resolve
from .deploy import DeployResult, deploy_to

log = logging.getLogger(__name__)

OUR_SCRIPT_ID: str = "reqable-mcp"
OUR_SCRIPT_NAME: str = "reqable-mcp"


@dataclass
class InstallPlan:
    """Everything ``install_hook`` will (or would) do.

    A plan is built without touching disk; ``apply()`` performs it.
    Use ``describe()`` for a human-readable diff to print under
    ``--dry-run``.
    """

    paths: Paths
    venv_python: Path
    hook_dir: Path
    backup_capture_config: Path
    backup_script_environment: Path
    new_script_entry: dict
    new_script_environment: dict
    deploy_result: DeployResult | None = None
    backups_made: list[Path] = field(default_factory=list)

    def describe(self) -> str:
        lines = [
            "Install plan:",
            f"  hook dir       : {self.hook_dir}",
            f"  python         : {self.venv_python}",
            f"  capture_config : {self.paths.reqable_capture_config}",
            f"     -> backup   : {self.backup_capture_config}",
            f"  script_env     : {self.paths.reqable_script_environment}",
            f"     -> backup   : {self.backup_script_environment}",
            "  scriptConfig change:",
            "    set isEnabled = true",
            f"    add scripts[] entry id={self.new_script_entry.get('id')!r}",
            f"    set execHome = {self.hook_dir}",
            f"  script_environment new value: {self.new_script_environment}",
        ]
        return "\n".join(lines)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Tempfile + rename, keep 0600 perms."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _is_reqable_running() -> bool:
    """Check whether Reqable.app is currently running.

    Returns False (i.e. "looks safe") if pgrep is unavailable rather
    than blocking — we don't want to refuse installs on minimal
    systems that lack pgrep.
    """
    try:
        res = subprocess.run(
            ["pgrep", "-x", "Reqable"],
            capture_output=True, text=True, timeout=2.0,
        )
        return res.returncode == 0 and bool(res.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _python_executable() -> Path:
    """Path to the Python interpreter Reqable should use to run hooks.

    Prefers ``sys.executable`` (this is the venv we're running in if
    install was triggered via ``reqable-mcp install-hook``). Falls
    back to ``/usr/bin/env python3`` if for some reason that isn't a
    real file.
    """
    p = Path(sys.executable)
    if p.is_file():
        return p
    return Path("/usr/bin/env")


def build_script_entry(*, hook_dir: Path) -> dict:
    """Build the JSON object to put into ``scriptConfig.scripts[]``.

    Best-effort schema inferred from peers like ``rewriteConfig``;
    Reqable hasn't documented this. If a future Reqable build rejects
    this shape, generate a sample by hand in the UI and adjust.
    """
    return {
        "id": OUR_SCRIPT_ID,
        "name": OUR_SCRIPT_NAME,
        "method": "",          # any method
        "url": "*",            # any URL
        "wildcard": True,      # treat url as wildcard pattern
        "isEnabled": True,
        "path": str(hook_dir), # where main.py / addons.py live
    }


def build_script_environment(*, executor: Path) -> dict:
    """Mirror Reqable's own ``script_environment`` shape.

    Observed live values: ``{"home": "", "version": "3.9.6", "executor": "python3"}``.
    We point ``executor`` at our venv so addons see the same
    environment everything else in the project runs under.
    """
    return {
        "executor": str(executor),
        "version": ".".join(map(str, sys.version_info[:3])),
        "home": "",
    }


def make_plan(paths: Paths | None = None) -> InstallPlan:
    """Compute the plan without touching disk.

    Caller can ``print(plan.describe())`` for dry-run, or pass the
    plan to :func:`apply_plan` to commit.
    """
    p = paths or resolve()
    p.ensure_our_dirs()
    p.our_backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    ts = int(time.time())
    venv_py = _python_executable()
    return InstallPlan(
        paths=p,
        venv_python=venv_py,
        hook_dir=p.our_hook_dir,
        backup_capture_config=p.our_backup_dir / f"capture_config.{ts}.bak",
        backup_script_environment=p.our_backup_dir / f"script_environment.{ts}.bak",
        new_script_entry=build_script_entry(hook_dir=p.our_hook_dir),
        new_script_environment=build_script_environment(executor=venv_py),
    )


def apply_plan(plan: InstallPlan, *, force_running: bool = False) -> InstallPlan:
    """Execute the plan. Returns the same plan with side-effect fields filled."""
    if _is_reqable_running() and not force_running:
        raise RuntimeError(
            "Reqable.app is currently running — quit it before installing the "
            "hook (Reqable rewrites capture_config on its own; modifying it "
            "concurrently risks losing changes). Re-run with force_running=True "
            "to override."
        )
    p = plan.paths
    p.assert_reqable_present()
    if not p.reqable_capture_config.exists():
        raise FileNotFoundError(
            f"capture_config not found at {p.reqable_capture_config}. "
            "Has Reqable been launched at least once?"
        )
    if not p.reqable_script_environment.exists():
        raise FileNotFoundError(
            f"script_environment not found at {p.reqable_script_environment}."
        )

    # 1. Backup the two files (cp; permissions preserved).
    shutil.copy2(p.reqable_capture_config, plan.backup_capture_config)
    plan.backups_made.append(plan.backup_capture_config)
    shutil.copy2(p.reqable_script_environment, plan.backup_script_environment)
    plan.backups_made.append(plan.backup_script_environment)
    log.info("backed up to %s", p.our_backup_dir)

    # 2. Deploy hook templates.
    plan.deploy_result = deploy_to(plan.hook_dir)
    log.info("hook deployed: written=%s skipped=%s",
             plan.deploy_result.written, plan.deploy_result.skipped)

    # 3. Modify capture_config.
    with p.reqable_capture_config.open() as f:
        cap = json.load(f)
    sc = cap.setdefault("scriptConfig", {"scripts": [], "isEnabled": False, "execHome": ""})
    scripts = sc.setdefault("scripts", [])
    # Replace any pre-existing reqable-mcp entry; leave others alone.
    sc["scripts"] = [s for s in scripts if s.get("id") != OUR_SCRIPT_ID]
    sc["scripts"].append(plan.new_script_entry)
    sc["isEnabled"] = True
    sc["execHome"] = str(plan.hook_dir)
    _atomic_write_json(p.reqable_capture_config, cap)

    # 4. Modify script_environment.
    _atomic_write_json(p.reqable_script_environment, plan.new_script_environment)

    return plan


def uninstall_hook(paths: Paths | None = None) -> dict:
    """Remove our entry from capture_config and restore script_environment.

    Returns a small dict summary suitable for printing.
    """
    p = paths or resolve()
    if not p.reqable_capture_config.exists():
        return {"status": "no-op", "reason": "capture_config missing"}

    summary: dict = {"removed_script": False, "restored_script_environment": False}

    # 1. Strip our entry from scriptConfig.scripts. Don't touch other scripts.
    with p.reqable_capture_config.open() as f:
        cap = json.load(f)
    sc = cap.get("scriptConfig") or {}
    scripts = sc.get("scripts") or []
    new_scripts = [s for s in scripts if s.get("id") != OUR_SCRIPT_ID]
    if len(new_scripts) != len(scripts):
        sc["scripts"] = new_scripts
        # Only flip isEnabled off if we leave no scripts at all.
        if not new_scripts:
            sc["isEnabled"] = False
            sc["execHome"] = ""
        cap["scriptConfig"] = sc
        _atomic_write_json(p.reqable_capture_config, cap)
        summary["removed_script"] = True

    # 2. Try to restore script_environment from the most recent backup.
    if p.our_backup_dir.exists():
        backups = sorted(
            p.our_backup_dir.glob("script_environment.*.bak"),
            key=lambda b: b.stat().st_mtime,
        )
        if backups:
            shutil.copy2(backups[-1], p.reqable_script_environment)
            summary["restored_script_environment"] = True
            summary["restored_from"] = str(backups[-1])

    return summary


def find_latest_backup(paths: Paths, prefix: str) -> Path | None:
    """Return the most recent backup file matching ``prefix.*.bak``."""
    if not paths.our_backup_dir.exists():
        return None
    candidates = sorted(
        paths.our_backup_dir.glob(f"{prefix}.*.bak"),
        key=lambda b: b.stat().st_mtime,
    )
    return candidates[-1] if candidates else None


def restore_capture_config(paths: Paths | None = None) -> Path | None:
    """Full restore from the most recent capture_config backup.

    Used when the surgical uninstall isn't enough (e.g. user wants to
    revert to an exact snapshot). Returns the backup file used, or
    None if no backup is present.
    """
    p = paths or resolve()
    backup = find_latest_backup(p, "capture_config")
    if backup is None:
        return None
    shutil.copy2(backup, p.reqable_capture_config)
    return backup


__all__ = [
    "OUR_SCRIPT_ID",
    "OUR_SCRIPT_NAME",
    "InstallPlan",
    "apply_plan",
    "build_script_entry",
    "build_script_environment",
    "find_latest_backup",
    "make_plan",
    "restore_capture_config",
    "uninstall_hook",
]
