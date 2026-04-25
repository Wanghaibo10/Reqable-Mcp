#!/usr/bin/env bash
# reqable-mcp uninstaller — reverses install.sh.
#
# Touches ONLY:
#   * ~/.claude/mcp.json (removes the "reqable" entry)
#   * ~/.reqable-mcp/    (deletes; user data + cache)
# Never touches any Reqable file.

set -euo pipefail

CLAUDE_MCP_JSON="${HOME}/.claude/mcp.json"
DATA_DIR="${HOME}/.reqable-mcp"

echo "==> reqable-mcp uninstaller"

# ---------------------------------------------------------------- 1. unregister MCP

if [[ -f "$CLAUDE_MCP_JSON" ]]; then
  echo "==> removing 'reqable' entry from ${CLAUDE_MCP_JSON}"
  PY=$(command -v python3 || true)
  if [[ -z "$PY" ]]; then
    echo "    no python3; please remove the entry manually" >&2
  else
    MCP_JSON="$CLAUDE_MCP_JSON" "$PY" - <<'PY'
import json, os, sys
path = os.environ["MCP_JSON"]
try:
    with open(path) as f:
        data = json.load(f)
except (json.JSONDecodeError, OSError):
    sys.exit(0)
servers = data.get("mcpServers")
if isinstance(servers, dict) and "reqable" in servers:
    del servers["reqable"]
    if not servers:
        del data["mcpServers"]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    print("    entry removed")
else:
    print("    no 'reqable' entry to remove")
PY
  fi
else
  echo "    no ${CLAUDE_MCP_JSON} found, skipping"
fi

# ---------------------------------------------------------------- 2. data dir

if [[ -d "$DATA_DIR" ]]; then
  echo "==> removing ${DATA_DIR}"
  rm -rf "$DATA_DIR"
fi

echo
echo "✅ uninstall complete (Reqable data/config untouched)"
echo
echo "If you also want to drop the Python virtualenv, run:"
echo "    rm -rf $(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.venv"
