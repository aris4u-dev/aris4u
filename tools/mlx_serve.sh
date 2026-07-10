#!/usr/bin/env bash
# Cuerpo local: arranca/para el Mistral-Small-3.2-24B (occidental) vía mlx_lm.server.
# (SWAP 2026-07-01: era Qwen3.6-35B-A3B chino; política anti-IA-china del dueño.)
#
# LAZY + RAM-GATED por diseño: solo arranca si hay RAM suficiente (~12GB en 4bit NO debe
# competir con Claude). El router (engine/v16 dispatch_mlx) es health-aware: si este
# server NO corre, route_local cae a Foundation-Sec/W2 (fail-open). Así el cuerpo cede
# memoria a Claude cuando no se usa (disciplina anti-saturación de ~/.claude/rules).
#
# Uso: tools/mlx_serve.sh {start|stop|status}
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv312/bin/python"
MODEL="${ARIS4U_MLX_MODEL:-mlx-community/Mistral-Small-3.2-24B-Instruct-2506-4bit}"
PORT="${ARIS4U_MLX_PORT:-8765}"
MIN_FREE_GB="${ARIS4U_MLX_MIN_FREE_GB:-16}"  # Mistral-24B 4bit ~12GB + margen (antes 26 para el MoE ~23GB)
PIDFILE="$ROOT/data/mlx_server.pid"
LOG="$ROOT/logs/mlx_server.log"

free_gb() {
  vm_stat | awk '/Pages free/{f=$3} /Pages inactive/{i=$3} END{gsub(/\./,"",f);gsub(/\./,"",i); printf "%.0f", (f+i)*16384/1073741824}'
}

case "${1:-status}" in
  start)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "ya corriendo (pid $(cat "$PIDFILE"))"; exit 0
    fi
    fr="$(free_gb)"
    if [ "$fr" -lt "$MIN_FREE_GB" ]; then
      echo "RAM insuficiente: ${fr}GB libres < ${MIN_FREE_GB}GB requeridos — NO arranco (cede RAM a Claude)"; exit 1
    fi
    mkdir -p "$ROOT/logs"
    echo "arrancando cuerpo local: $MODEL en :$PORT (${fr}GB libres)..."
    nohup "$VENV" -m mlx_lm.server --model "$MODEL" --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "pid $(cat "$PIDFILE") · log $LOG (el primer request carga el modelo, ~30-60s)"
    ;;
  stop)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE"; echo "cuerpo local parado (RAM liberada)"
    else
      echo "no estaba corriendo"; rm -f "$PIDFILE" 2>/dev/null || true
    fi
    ;;
  status)
    if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "corriendo (pid $(cat "$PIDFILE")) · $(free_gb)GB libres"
    else
      echo "parado"
    fi
    curl -s --max-time 3 "http://localhost:$PORT/v1/models" 2>/dev/null | head -c 300 || true
    echo
    ;;
  *)
    echo "uso: $0 {start|stop|status}"; exit 2
    ;;
esac
