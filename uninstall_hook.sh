#!/usr/bin/env bash
# Reverse install_hook.sh — removes our entry from Reqable's
# capture_config.scriptConfig.scripts[] and restores
# script_environment from the most recent backup.
#
# Does NOT delete ~/.reqable-mcp/hook/ (kept around for inspection
# and so re-installing is fast). Run uninstall.sh to remove our
# data dir entirely.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_BIN="${PROJECT_DIR}/.venv/bin/reqable-mcp"

if [[ ! -x "$VENV_BIN" ]]; then
  echo "❌ ${VENV_BIN} not found. Was reqable-mcp installed?" >&2
  exit 1
fi

exec "$VENV_BIN" uninstall-hook "$@"
