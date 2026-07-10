#!/bin/bash
# FROZEN (Fable-Gate 2026-07-05): shadow-only, nunca activado; cron descargado. Recargar solo con demanda real.
# run_daily.sh — orquestador diario de auto-adaptación de ARIS4U (Paso 7c).
#
# Modo vía ARIS4U_AUTOUPDATE:  off | shadow (default) | pr-only | auto
#   off     : no hace nada (kill-switch).
#   shadow  : detecta cambios + corre el GATE + registra qué HARÍA. NO modifica nada. (rodaje seguro)
#   pr-only : (no implementado aún) aplicaría en rama + abriría PR para todo, sin auto-merge.
#   auto    : (no implementado aún) auto-aplicaría lo mecánico que pase el gate; semántico -> PR.
#
# Disciplina: un solo proceso (lock), nunca en paralelo; NO ejecuta aris_dialectic (Ollama);
# NUNCA toca main directamente (cuando se implemente auto/pr usará ramas adapt/auto-<date> + PR).
# Telemetría a logs/adapt.jsonl. Cosecha por watch_sources + smoke_test (gate).
set -o pipefail
ARIS4U_ROOT="${ARIS4U_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}}"
export ARIS4U_ROOT
VENV="$ARIS4U_ROOT/.venv312/bin/python3"
MODE="${ARIS4U_AUTOUPDATE:-shadow}"
LOG="$ARIS4U_ROOT/logs/adapt.jsonl"
LOCK="/tmp/aris4u_adapt.lock"

[ "$MODE" = "off" ] && exit 0
[ -x "$VENV" ] || exit 0
mkdir -p "$ARIS4U_ROOT/logs" 2>/dev/null

# Un solo proceso (anti-saturación M5)
if [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then exit 0; fi
echo $$ > "$LOCK"; trap 'rm -f "$LOCK"' EXIT

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
_log() { printf '%s\n' "$1" >> "$LOG" 2>/dev/null || true; }

# 0. Smoke roundtrip de memoria (FREEZE ítem 1) — corre SIEMPRE, antes del early-exit
#    por "sin cambios": el write-path puede romperse en silencio aunque nada cambie en
#    las fuentes. Su propio log vive en logs/smoke_roundtrip.jsonl; aquí solo se anota
#    el veredicto. NO aborta run_daily si falla (independiente de la auto-adaptación).
if "$VENV" "$ARIS4U_ROOT/tools/smoke_roundtrip.py" >/dev/null 2>&1; then
    _log "{\"ts\":\"$TS\",\"event\":\"smoke_roundtrip\",\"ok\":true}"
else
    _log "{\"ts\":\"$TS\",\"event\":\"smoke_roundtrip\",\"ok\":false}"
fi

# 1. Detectar cambios en las fuentes de Claude
DELTAS=$("$VENV" "$ARIS4U_ROOT/tools/adapt/watch_sources.py" 2>/dev/null)
CHANGED=$(printf '%s' "$DELTAS" | "$VENV" -c "import sys,json;print(json.load(sys.stdin).get('changed'))" 2>/dev/null)

if [ "$CHANGED" != "True" ]; then
    _log "{\"ts\":\"$TS\",\"event\":\"adapt_check\",\"mode\":\"$MODE\",\"changed\":false}"
    exit 0
fi

# 2. Correr el GATE (contrato intacto) ANTES de considerar cualquier acción
"$VENV" "$ARIS4U_ROOT/tools/adapt/smoke_test.py" >/dev/null 2>&1; GATE=$?
GATE_PASS=$([ "$GATE" -eq 0 ] && echo true || echo false)

# 3. Contar deltas por ruta
MECH=$(printf '%s' "$DELTAS" | "$VENV" -c "import sys,json;print(len(json.load(sys.stdin).get('mechanical',[])))" 2>/dev/null)
SEM=$(printf '%s' "$DELTAS"  | "$VENV" -c "import sys,json;print(len(json.load(sys.stdin).get('semantic',[])))" 2>/dev/null)
SRCS=$(printf '%s' "$DELTAS" | "$VENV" -c "import sys,json;print(','.join(d['source'] for d in json.load(sys.stdin).get('deltas',[])))" 2>/dev/null)

case "$MODE" in
    shadow)
        # Solo reporta qué haría. NO actualiza baseline (seguir alertando hasta que se actúe).
        _log "{\"ts\":\"$TS\",\"event\":\"adapt_shadow\",\"changed\":true,\"gate_pass\":$GATE_PASS,\"sources\":\"$SRCS\",\"mechanical\":$MECH,\"semantic\":$SEM,\"would\":\"mechanical->rama+gate+auto-merge; semantic->claude -p headless + PR\"}"
        echo "[ARIS4U adapt:shadow] cambios detectados ($SRCS) — gate_pass=$GATE_PASS, mech=$MECH, sem=$SEM. Reporte en $LOG. (modo sombra: no se aplicó nada)" >&2
        ;;
    pr-only)
        # Delega en pr_pilot.py: crea rama adapt/auto-<fecha>, commit, push, gh pr create.
        # Rollback automático si cualquier paso falla. NUNCA merge a main.
        "$VENV" "$ARIS4U_ROOT/tools/adapt/pr_pilot.py" \
            --deltas-json "$DELTAS" \
            --gate-pass "$GATE_PASS"
        PR_RC=$?
        if [ "$PR_RC" -eq 0 ]; then
            _log "{\"ts\":\"$TS\",\"event\":\"adapt_pr_dispatched\",\"mode\":\"$MODE\",\"sources\":\"$SRCS\",\"gate_pass\":$GATE_PASS}"
        else
            _log "{\"ts\":\"$TS\",\"event\":\"adapt_pr_failed\",\"mode\":\"$MODE\",\"sources\":\"$SRCS\",\"gate_pass\":$GATE_PASS,\"rc\":$PR_RC}"
        fi
        ;;
    auto)
        # Guard de seguridad: auto-apply se implementa en Tramo 4 tras rodaje de pr-only.
        _log "{\"ts\":\"$TS\",\"event\":\"adapt_mode_not_implemented\",\"mode\":\"$MODE\",\"sources\":\"$SRCS\"}"
        echo "[ARIS4U adapt] modo 'auto' aún NO implementado (usar pr-only primero). No se aplicó nada." >&2
        ;;
    *)
        _log "{\"ts\":\"$TS\",\"event\":\"adapt_unknown_mode\",\"mode\":\"$MODE\"}"
        ;;
esac
exit 0
