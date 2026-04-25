#!/usr/bin/env bash
# reqable-mcp Phase-2 hook installer.
#
# What this does:
#   1. Confirms Reqable.app is not running (unless --force)
#   2. Copies our addons template into ~/.reqable-mcp/hook/
#   3. Backs up Reqable's capture_config + script_environment
#   4. Adds a "reqable-mcp" entry into capture_config.scriptConfig.scripts[]
#      and flips scriptConfig.isEnabled = true
#   5. Points Reqable's script_environment at our venv's python3
#
# Reverse with ./uninstall_hook.sh — removes the entry and restores
# script_environment from the latest backup.
#
# Use --dry-run to see the plan without writing.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_BIN="${PROJECT_DIR}/.venv/bin/reqable-mcp"

if [[ ! -x "$VENV_BIN" ]]; then
  echo "❌ ${VENV_BIN} not found. Run ./install.sh first." >&2
  exit 1
fi

# Pass through CLI flags (--dry-run / --force) verbatim.
exec "$VENV_BIN" install-hook "$@"
