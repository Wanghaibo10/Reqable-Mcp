"""Deploy the addons template into a target directory.

Used by ``install_hook.sh`` (M13) and from tests. We copy three files
verbatim from :mod:`reqable_mcp.hook.template`:

* ``main.py``    — Reqable's entry point.
* ``reqable.py`` — Reqable's SDK.
* ``addons.py``  — our daemon-talking shell.

Behavior:

* **Idempotent**. If the target file is byte-identical to the template,
  we skip rewriting it. If it differs we overwrite (preserves the
  installer's safety: rerunning ``install_hook.sh`` after we ship a
  new addons.py replaces it).
* **Atomic** per-file. A tempfile is written and renamed; readers
  never see a half-written file.
* **0700 dir / 0600 files** — Reqable runs as the same user we do, so
  the strict perms suffice.
"""

from __future__ import annotations

import contextlib
import importlib.resources
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# The three files we ship. Order matters only for log output.
TEMPLATE_FILES: tuple[str, ...] = ("main.py", "reqable.py", "addons.py")


@dataclass
class DeployResult:
    """Summary returned by :func:`deploy_to`."""

    target_dir: Path
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.written)


def _read_template(name: str) -> bytes:
    """Read a packaged template file as bytes.

    Uses ``importlib.resources`` so it works whether the package is
    installed editable, from a wheel, or zipped.
    """
    pkg = importlib.resources.files("reqable_mcp.hook.template")
    return (pkg / name).read_bytes()


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def deploy_to(target_dir: Path) -> DeployResult:
    """Copy the three template files into ``target_dir``.

    Creates the directory if missing (0700). Returns a
    :class:`DeployResult` listing which files were written vs. skipped
    because they were already up to date.
    """
    target = Path(target_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Tighten perms even if pre-existing.
    try:
        os.chmod(target, 0o700)
    except PermissionError:
        log.warning("could not chmod %s to 0700", target)

    result = DeployResult(target_dir=target)
    for name in TEMPLATE_FILES:
        src_bytes = _read_template(name)
        dst = target / name
        if dst.exists():
            try:
                if dst.read_bytes() == src_bytes:
                    result.skipped.append(name)
                    continue
            except OSError:
                # If we can't read it, fall through and overwrite.
                pass
        _atomic_write(dst, src_bytes)
        result.written.append(name)
        log.info("deployed %s -> %s", name, dst)
    return result


__all__ = ["DeployResult", "TEMPLATE_FILES", "deploy_to"]
