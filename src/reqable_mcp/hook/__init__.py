"""Hook templates and the deploy helper.

Reqable runs hooks by ``fork``-ing a Python interpreter per request and
giving it a ``main.py`` to invoke. The interpreter exits within a
couple of hundred milliseconds, so any heavy state (rule definitions,
hit logs) lives in the long-running daemon and ``addons.py`` is a thin
shell that asks the daemon over the IPC socket each time.

The ``template/`` subdirectory holds three files:

* ``main.py``     — Reqable's own entry point (verbatim, do not edit).
* ``reqable.py``  — Reqable's SDK (verbatim, do not edit).
* ``addons.py``   — our thin shell that talks to the daemon.

:func:`deploy_to` copies all three into a target directory (typically
``~/.reqable-mcp/hook/``); ``install_hook.sh`` (M13) then registers
that directory in Reqable's ``capture_config``.
"""

from .deploy import DeployResult, deploy_to

__all__ = ["DeployResult", "deploy_to"]
