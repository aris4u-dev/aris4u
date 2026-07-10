#!/bin/bash
# ram-saturation-guard.sh — PreToolUse BLOQUEANTE: impide lanzar trabajo pesado
# (Workflow/Agent/Task) cuando el sistema ya está tenso de RAM/swap/load.
#
# Portado desde ~/.claude/hooks/ram-saturation-guard.sh para distribución como
# parte del plugin ARIS4U. Fuente versionada: hooks/standalone/ (plugin repo).
#
# Contexto: nace del incidente 2026-06-16 — 104 workflows/48h, sesiones de
# 79-92 agentes → RAM saturada → swap/thrashing → fans al máximo + API errors.
# Convierte la regla advisory de parallel-dispatch en defensa MECÁNICA.
#
# PORTABILIDAD:
#   - Usa $HOME (nunca paths hardcodeados de usuario).
#   - Las comprobaciones de RAM (vm_stat/sysctl) son macOS-específicas.
#     En Linux/Windows el guard hace FAIL-OPEN (exit 0, advisory únicamente)
#     para no bloquear entornos sin esas herramientas.
#   - Umbrales configurables vía env:
#       RAM_BLOCK_AVAIL_GB   (default 8)  — bloquea si disponible < N GB
#       RAM_BLOCK_SWAP_MB    (default 1024) — bloquea si swap usado > N MB
#       RAM_WARN_AVAIL_GB    (default 14)  — avisa amarillo si disponible < N GB
#       RAM_WARN_LOAD        (default 16)  — avisa amarillo si load-avg > N
#       RAM_MAX_WORKFLOWS    (default 2)   — bloquea si hay >= N workflows activos
#     Ajusta según la RAM total de tu máquina (defaults pensados para 48 GB).
#
# Salida: exit 2 = BLOQUEA (stderr = razón). exit 0 = permite (stdout = advisory).

set +e

INPUT=$(cat)

TOOL=$(echo "$INPUT" | python3 -c "
import sys,json
try: print(json.load(sys.stdin).get('tool_name',''))
except: print('')" 2>/dev/null)

# Solo trabajo pesado fan-out. Read/Bash/Edit no pasan por aquí.
case "$TOOL" in
    Workflow|Agent|Task) ;;
    *) exit 0 ;;
esac

# Umbrales (configurables vía env)
BLOCK_AVAIL_GB="${RAM_BLOCK_AVAIL_GB:-8}"
BLOCK_SWAP_MB="${RAM_BLOCK_SWAP_MB:-1024}"
WARN_AVAIL_GB="${RAM_WARN_AVAIL_GB:-14}"
WARN_LOAD="${RAM_WARN_LOAD:-16}"
MAX_WORKFLOWS="${RAM_MAX_WORKFLOWS:-2}"

# Inicializar a 0 para fail-open en non-macOS
AVAIL_GB=999
COMP_GB=0
SWAP_USED_MB=0
LOAD1_INT=0

# --- Lectura de memoria (vm_stat) — macOS solamente ---
if command -v vm_stat >/dev/null 2>&1 && command -v sysctl >/dev/null 2>&1; then
    # Detectar page size dinámicamente (macOS: 16384 en Apple Silicon, 4096 en Intel)
    PAGE=$(sysctl -n hw.pagesize 2>/dev/null || echo 16384)
    read FREE SPEC INACT PURGE COMP <<<"$(vm_stat 2>/dev/null | awk '
        /Pages free/                 {f=$3+0}
        /Pages speculative/          {s=$3+0}
        /Pages inactive/             {i=$3+0}
        /Pages purgeable/            {p=$3+0}
        /Pages occupied by compressor/ {c=$3+0}
        END {print f, s, i, p, c}')"

    # available = recuperable sin swapear (free + speculative + inactive + purgeable)
    AVAIL_GB=$(( (FREE + SPEC + INACT + PURGE) * PAGE / 1073741824 ))
    COMP_GB=$(( COMP * PAGE / 1073741824 ))

    # Swap usado (señal certera de thrashing)
    SWAP_USED_MB=$(sysctl -n vm.swapusage 2>/dev/null | sed -E 's/.*used = ([0-9.]+)M.*/\1/' | cut -d. -f1)
    [[ -z "$SWAP_USED_MB" ]] && SWAP_USED_MB=0

    # Load average 1-min
    LOAD1=$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}')
    LOAD1_INT=${LOAD1%.*}
    [[ -z "$LOAD1_INT" ]] && LOAD1_INT=0
fi

# --- Workflows ya activos (journal escrito en últimos 3 min) — chequeo barato ---
ACTIVE_WF=$(find "$HOME/.claude/projects" -path '*workflows/wf_*/journal.jsonl' -mmin -3 2>/dev/null | wc -l | tr -d ' ')

# ===================== DECISIÓN =====================
REASONS=""
[[ "$AVAIL_GB" -lt "$BLOCK_AVAIL_GB" ]] && REASONS+="RAM disponible ${AVAIL_GB}GB (<${BLOCK_AVAIL_GB}). "
[[ "$SWAP_USED_MB" -gt "$BLOCK_SWAP_MB" ]] && REASONS+="Swap ${SWAP_USED_MB}MB en uso (thrashing). "
[[ "$ACTIVE_WF" -ge "$MAX_WORKFLOWS" ]] && REASONS+="${ACTIVE_WF} workflows ya activos. "

if [[ -n "$REASONS" ]]; then
    echo "SATURACIÓN — no lances $TOOL ahora: ${REASONS}" >&2
    echo "Acumular fan-out → swap → thrashing → API errors → sesión muerta." >&2
    echo "Haz esto: (1) deja terminar lo que corre, (2) un workflow a la vez, (3) /compact si la sesión está larga. Re-intenta cuando RAM>${BLOCK_AVAIL_GB}GB y swap≈0." >&2
    # Telemetría de bloqueo (fail-open)
    GUARD_LOG="$HOME/.claude/logs/guard-blocks.jsonl"
    mkdir -p "$(dirname "$GUARD_LOG")" 2>/dev/null
    TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
    REASON_JSON=$(printf '%s' "$REASONS" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip()))" 2>/dev/null || echo '"ram-saturation"')
    printf '{"ts":"%s","guard":"ram-saturation","tool":"%s","reason":%s}\n' "$TS" "$TOOL" "$REASON_JSON" >> "$GUARD_LOG" 2>/dev/null
    exit 2
fi

# Zona amarilla: permite pero avisa
WARN=""
[[ "$AVAIL_GB" -lt "$WARN_AVAIL_GB" ]] && WARN+="RAM disponible ${AVAIL_GB}GB (compressor ${COMP_GB}GB). "
[[ "$LOAD1_INT" -ge "$WARN_LOAD" ]] && WARN+="load ${LOAD1} en $(sysctl -n hw.physicalcpu 2>/dev/null || echo '?') cores. "
[[ "$ACTIVE_WF" -ge 1 ]] && WARN+="${ACTIVE_WF} workflow activo — no acumules otro en paralelo. "
[[ -n "$WARN" ]] && echo "[HARDWARE] $TOOL permitido pero sistema tenso: ${WARN}El límite real es modelo/builds locales, no el conteo de agentes de razonamiento (esos son nube). Secuencia workflows."

exit 0
