#!/usr/bin/env bash
# reqable-mcp installer (MVP)
#
# What this does:
#   1. Verifies macOS + Python ≥ 3.10
#   2. Installs the `reqable-mcp` package (editable mode by default)
#   3. Creates ~/.reqable-mcp/ with 0700 perms
#   4. Adds the MCP server entry to ~/.claude/mcp.json
#
# What this does NOT do:
#   * Modify any Reqable data or configuration
#   * Install launchd plists or background services
#   * Require Reqable Pro
#
# Re-run is idempotent.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_MCP_JSON="${HOME}/.claude/mcp.json"
DATA_DIR="${HOME}/.reqable-mcp"

echo "==> reqable-mcp installer"
echo "    project: ${PROJECT_DIR}"

# ---------------------------------------------------------------- 1. checks

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "❌ macOS only (uname=$(uname -s))" >&2
  exit 1
fi

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>/dev/null; then
      PY="$cand"
      break
    fi
  fi
done

if [[ -z "$PY" ]]; then
  echo "❌ Python ≥ 3.10 not found. Install via 'brew install python@3.13'." >&2
  exit 1
fi
PY_VER=$("$PY" --version 2>&1)
echo "    using ${PY_VER} at $(command -v "$PY")"

# ---------------------------------------------------------------- 2. install package

if [[ -d "${PROJECT_DIR}/.venv" ]]; then
  echo "==> using existing .venv"
else
  echo "==> creating .venv"
  "$PY" -m venv "${PROJECT_DIR}/.venv"
fi

VENV_PIP="${PROJECT_DIR}/.venv/bin/pip"
VENV_BIN="${PROJECT_DIR}/.venv/bin/reqable-mcp"

echo "==> installing package (editable)"
"$VENV_PIP" install --upgrade pip >/dev/null
"$VENV_PIP" install -e "${PROJECT_DIR}" >/dev/null

if [[ ! -x "$VENV_BIN" ]]; then
  echo "❌ install succeeded but ${VENV_BIN} is missing" >&2
  exit 1
fi
echo "    binary: ${VENV_BIN}"

# ---------------------------------------------------------------- 3. data dir

if [[ ! -d "$DATA_DIR" ]]; then
  echo "==> creating ${DATA_DIR} (0700)"
  mkdir -p "$DATA_DIR"
fi
chmod 700 "$DATA_DIR" || true

# ---------------------------------------------------------------- 4. MCP registration

mkdir -p "$(dirname "$CLAUDE_MCP_JSON")"

REGISTER_PY=$(cat <<'PY'
import json, os, sys
path = os.environ["MCP_JSON"]
binary = os.environ["BIN"]
data = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"warning: existing {path} unreadable; rewriting", file=sys.stderr)
        data = {}
servers = data.setdefault("mcpServers", {})
existing = servers.get("reqable")
desired = {"command": binary, "args": ["serve"]}
if existing == desired:
    print("    already registered, no change")
    sys.exit(0)
servers["reqable"] = desired
tmp = path + ".tmp"
with open(tmp, "w") as f:
    json.dump(data, f, indent=2)
os.replace(tmp, path)
print(f"    wrote {path}")
PY
)

echo "==> registering MCP server in ${CLAUDE_MCP_JSON}"
MCP_JSON="$CLAUDE_MCP_JSON" BIN="$VENV_BIN" "$PY" -c "$REGISTER_PY"

# ---------------------------------------------------------------- done

echo
echo "✅ install complete"
echo
echo "Next steps:"
echo "  1. Make sure Reqable is open and capturing."
echo "  2. Restart Claude Code so it picks up the new MCP server."
echo "  3. In a Claude Code chat, try /mcp — 'reqable' should appear."
echo "     Or ask Claude to call tools like:"
echo "       - list_recent(limit=5)"
echo "       - wait_for(host='example.com')"
echo "       - find_dynamic_fields(host='target.com')"
echo
echo "  Sanity-check from the shell anytime with:"
echo "      ${VENV_BIN} status"
