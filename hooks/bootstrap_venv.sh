#!/bin/bash
# ARIS4U venv bootstrap — auto-instalación del entorno para el flujo "producto nativo".
#
# Contexto: cuando ARIS4U se instala como plugin de Claude Code desde un marketplace git
# (`/plugin install aris4u@…`), ${CLAUDE_PLUGIN_ROOT} apunta al clon del plugin, que NO
# trae venv. Los hooks (hooks.json) invocan `${CLAUDE_PLUGIN_ROOT}/.venv312/bin/python3`,
# así que sin venv el sistema no arranca. Este script, cableado como PRIMER hook de
# SessionStart, crea ese venv UNA sola vez en la primera sesión.
#
# Disciplina de seguridad:
#   - No-op INSTANTÁNEO si ya existe cualquier venv (.venv / .venv312 / .venv313 / .venv314)
#     — el mismo orden que integrations/mcp_wrapper.sh. Para una máquina que ya tenía
#     ARIS4U (p. ej. la del autor) esto sale con exit 0 sin tocar nada, costo ~0.
#   - Solo actúa en instalación fresca (venv completamente ausente).
#   - FAIL-OPEN TOTAL: cualquier fallo sale con exit 0 y un mensaje a stderr; jamás
#     bloquea el arranque de la sesión.
set -u

ROOT="${ARIS4U_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"

# 1) ¿Ya hay venv? → no-op instantáneo (mismo orden de búsqueda que el wrapper MCP).
for v in "$ROOT/.venv" "$ROOT/.venv312" "$ROOT/.venv313" "$ROOT/.venv314"; do
  [ -x "$v/bin/python3" ] && exit 0
done

# 2) Instalación fresca: se necesita python3 del sistema.
if ! command -v python3 >/dev/null 2>&1; then
  echo "aris4u: instala Python 3.11+ (3.12 recomendado) y reinicia la sesión para completar la instalación." >&2
  exit 0
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; then
  echo "aris4u: se requiere Python >= 3.11 en PATH; el bootstrap del entorno se omite." >&2
  exit 0
fi

# 3) Crear .venv312 + dependencias (misma lógica que install.sh, idempotente).
VENV="$ROOT/.venv312"
echo "aris4u: primera vez — creando el entorno local (.venv312 + dependencias, ~30-60s, una sola vez)…" >&2
if ! python3 -m venv "$VENV" >/dev/null 2>&1; then
  echo "aris4u: no se pudo crear el venv — corre 'bash install.sh' en $ROOT" >&2
  exit 0
fi
"$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
if "$VENV/bin/pip" install -q -e "$ROOT" >/dev/null 2>&1; then
  echo "aris4u: entorno listo — ARIS4U activo desde la próxima interacción." >&2
else
  echo "aris4u: 'pip install -e .' falló — corre 'bash install.sh' en $ROOT para diagnosticar." >&2
fi
exit 0
