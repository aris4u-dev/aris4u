#!/bin/bash
# hooks/async_vacuum.sh — async trigger for sessions.db delete pass
# Per V16.6 W4.4. Invoke from session_end.sh (Stop hook) with `&`.

set -euo pipefail

LAST_RUN_FILE="/tmp/aris4u_last_vacuum"
THROTTLE_SEC=3600  # 1h

# Throttle: skip if <1h since last
if [ -f "$LAST_RUN_FILE" ]; then
  last=$(cat "$LAST_RUN_FILE")
  now=$(date +%s)
  if [ $((now - last)) -lt "$THROTTLE_SEC" ]; then
    exit 0  # silently skip
  fi
fi

# Async run, disowned
ARIS4U_ROOT="${ARIS4U_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
# Producción usa .venv312; CI/entornos sin él caen al python3 del PATH.
PYBIN="$ARIS4U_ROOT/.venv312/bin/python"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3)"
nohup "$PYBIN" \
  "$ARIS4U_ROOT/tools/vacuum_sessions.py" \
  --db "$ARIS4U_ROOT/data/sessions.db" \
  --mode delete \
  >/dev/null 2>&1 &
disown

date +%s > "$LAST_RUN_FILE"
exit 0
