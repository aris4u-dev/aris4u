#!/bin/bash
# hooks/nightly_vacuum.sh — daily 02:30 UTC cron/systemd entry
# Per V16.6 W4.4. Runs delete + incremental vacuum.

set -euo pipefail

ARIS4U_ROOT="${ARIS4U_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
# Producción usa .venv312; CI/entornos sin él caen al python3 del PATH.
PYBIN="$ARIS4U_ROOT/.venv312/bin/python"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3)"
"$PYBIN" \
  "$ARIS4U_ROOT/tools/vacuum_sessions.py" \
  --db "$ARIS4U_ROOT/data/sessions.db" \
  --mode all

# V2.0: encender el termómetro del freeze — calificar la utilidad IMPLÍCITA (costo cero)
# de los recalls de la última semana y persistir en recall_feedback. Idempotente y respeta
# las marcas manuales (--mark). Corre ANTES de rotar el log para ver la ventana completa.
"$PYBIN" \
  "$ARIS4U_ROOT/tools/recall_usefulness.py" \
  --apply --days 7 || true

# V2.0: rotar el event log (llegó a 87MB sin rotación; log_rotator estaba des-cableado)
"$PYBIN" \
  "$ARIS4U_ROOT/tools/log_rotator.py" \
  --log-file "$ARIS4U_ROOT/logs/v16.1-events.jsonl" \
  --threshold-mb 50 || true
