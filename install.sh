#!/bin/bash
# install.sh — instalador idempotente de ARIS4U como plugin de Claude Code (Paso 8).
#
# Uso:
#   bash install.sh            # setup: venv + deps + gate + config de usuario (aris4u init)
#   bash install.sh --cron     # además instala el cron de auto-adaptación (modo SOMBRA)
#   bash install.sh --yes      # aris4u init no-interactivo (auto-detecta hardware/proyectos)
#   bash install.sh --no-init  # omite aris4u init (no genera ~/.aris4u/config.json)
#
# Seguro y re-ejecutable. NO modifica tu ~/.claude/settings.json (ver NOTA al final).
set -euo pipefail
ARIS4U_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ARIS4U_ROOT"

# Parseo de flags (en cualquier orden).
DO_CRON=0; INIT_YES=0; NO_INIT=0
for _arg in "$@"; do
    case "$_arg" in
        --cron) DO_CRON=1 ;;
        --yes) INIT_YES=1 ;;
        --no-init) NO_INIT=1 ;;
        *) echo "  [warn] flag desconocido: $_arg" ;;
    esac
done

echo "== ARIS4U install =="
echo "Root: $ARIS4U_ROOT"

# 0. Pre-check: Python >= 3.11 (pyproject requires-python; 3.12 recomendado)
if ! command -v python3 >/dev/null 2>&1; then
    echo "  [FAIL] python3 no está en el PATH — instala Python 3.11+ (3.12 recomendado)"; exit 1
fi
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)'; then
    echo "  [FAIL] Python $PYVER detectado; se requiere >= 3.11 (3.12 recomendado)"; exit 1
fi
echo "  [ok] Python $PYVER"

# 1. venv + dependencias (sqlite-vec ya está en pyproject; idempotente)
VENV="$ARIS4U_ROOT/.venv312"
if [ ! -x "$VENV/bin/python3" ]; then
    echo "Creando venv (.venv312)..."
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$VENV/bin/pip" install -q -e . && echo "  [ok] dependencias (pip install -e .)"

# 2. Ollama local (opcional — degrada limpio si no está)
if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "  [ok] Ollama local detectado (embeddings + dialéctica disponibles)"
else
    echo "  [warn] Ollama no detectado — memoria semántica/dialéctica degradan; FTS5 + guards siguen funcionando"
fi

# 3. Gate: el contrato carga
echo "Verificando contrato (smoke-test)..."
"$VENV/bin/python" "$ARIS4U_ROOT/tools/adapt/smoke_test.py" || { echo "  [FAIL] gate del contrato — revisar antes de usar"; exit 1; }

# 3.5 Config de usuario (aris4u init): desacopla hardware/clientes/labs de esta máquina.
#     Idempotente: si la config ya existe, no la pisa. Esto es lo que hace a ARIS4U
#     instalable por terceros sin editar código.
CONFIG_PATH="${ARIS4U_CONFIG:-$HOME/.aris4u/config.json}"
if [ "$NO_INIT" = "1" ]; then
    echo "  [skip] aris4u init omitido (--no-init); corre 'python3 tools/aris4u_init.py' cuando quieras"
elif [ -f "$CONFIG_PATH" ]; then
    echo "  [ok] config de usuario ya existe ($CONFIG_PATH) — regenera con 'python3 tools/aris4u_init.py --force'"
else
    echo "Generando config de usuario (aris4u init)..."
    # No-interactivo si se pidió --yes o si no hay TTY (CI). Fail-open: nunca aborta el install.
    if [ "$INIT_YES" = "1" ] || [ ! -t 0 ]; then
        "$VENV/bin/python" "$ARIS4U_ROOT/tools/aris4u_init.py" --yes \
            || echo "  [warn] aris4u init falló — corre 'python3 tools/aris4u_init.py' a mano"
    else
        "$VENV/bin/python" "$ARIS4U_ROOT/tools/aris4u_init.py" \
            || echo "  [warn] aris4u init falló — corre 'python3 tools/aris4u_init.py' a mano"
    fi
fi

# 4. Cron de auto-adaptación (opt-in, arranca en SOMBRA)
if [ "$DO_CRON" = "1" ]; then
    PLIST="$HOME/Library/LaunchAgents/com.aris4u.adapt-daily.plist"
    sed -e "s#__ARIS4U_ROOT__#$ARIS4U_ROOT#g" -e "s#__HOME__#$HOME#g" \
        "$ARIS4U_ROOT/tools/adapt/adapt-daily.plist.template" > "$PLIST"
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST" && echo "  [ok] cron auto-adaptación cargado (ARIS4U_AUTOUPDATE=shadow)"
fi

cat <<EOF

[done] ARIS4U setup completo.

Config de usuario: $CONFIG_PATH
  (hardware/clientes/labs de ESTA máquina; edítala o regenera con tools/aris4u_init.py --force)

Registrar como plugin en Claude Code:
  claude plugin marketplace add "$ARIS4U_ROOT"   # o el marketplace git del equipo
  claude plugin install aris4u@aris4u-dev
Los hooks (hooks/hooks.json) y el MCP (.mcp.json) se cargan por convención del plugin.

NOTA — solo si esta máquina YA tenía ARIS4U cableado a mano en ~/.claude/settings.json:
  al instalarlo como plugin, los 24 hooks correrían DOS veces (settings.json + plugin).
  Quita los bloques aris4u de settings.json (con backup) DESPUÉS de confirmar que el
  plugin carga. Operación consciente — este instalador NO la hace por ti.
EOF
