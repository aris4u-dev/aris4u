#!/bin/bash
# Wrapper for ARIS4U MCP server (FastMCP, stdio).
# Auto-detects active venv (.venv → .venv312 → .venv313 → .venv314) so cleanup
# scripts that rename/remove venvs don't silently break the MCP connection.
# History: hard-coded .venv path broke after 0424 cleanup; fixed 0430.
# Suppresses stderr duplication from MCP SDK.

set -e
ROOT="${ARIS4U_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
export ARIS4U_ROOT="$ROOT"

PY=""
for venv in "$ROOT/.venv" "$ROOT/.venv312" "$ROOT/.venv313" "$ROOT/.venv314"; do
  if [ -x "$venv/bin/python3" ]; then
    PY="$venv/bin/python3"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "aris4u mcp_wrapper: no venv found under $ROOT (.venv / .venv312 / .venv313 / .venv314)" >&2
  exit 1
fi

exec "$PY" -u "$ROOT/integrations/mcp_server.py" 2>>"$ROOT/data/mcp_server.stderr.log"
