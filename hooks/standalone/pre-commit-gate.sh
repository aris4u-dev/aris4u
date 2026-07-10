#!/bin/bash
# pre-commit-gate.sh — PreToolUse: mitad MECÁNICA automática del gate /second-auditor.
# Bloquea un `git commit` si los archivos STAGED de un stack con check rápido+fiable
# tienen ERRORES (no warnings/estilo). La mitad cara (agentes read-only + suite de tests)
# sigue en la skill /second-auditor invocada a mano.
#
# Originates from session 2026-06-25: 665 errors from `dart analyze` que la disciplina
# manual no atrapaba habrían sido bloqueados aquí. Ver feedback_ide_plus_second_auditor.
#
# Stacks con BLOQUEO (check rápido por archivo staged): Dart (dart analyze), Python
# (py_compile), Shell (bash -n). Otros stacks → solo AVISO para correr /second-auditor.
# Escape hatch: `git commit --no-verify` salta el gate por completo.
# Solo actúa en git commit; cualquier otro Bash sale en exit 0 inmediato (~0 overhead).
#
# Portabilidad: sin paths hardcodeados; usa herramientas del PATH del usuario.
# Fuente versionada: hooks/standalone/pre-commit-gate.sh (ARIS4U plugin).

INPUT=$(cat)

TOOL=$(printf '%s' "$INPUT" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('tool_name',''),end='')
except Exception: pass" 2>/dev/null)
CMD=$(printf '%s' "$INPUT" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('tool_input',{}).get('command',''),end='')
except Exception: pass" 2>/dev/null)

[[ "$TOOL" != "Bash" ]] && exit 0
# Solo git commit (incluye encadenados con ; && |). Si no, fuera.
echo "$CMD" | grep -qE '(^|[;&|][[:space:]]*)git[[:space:]]+commit' || exit 0
# Escape explícito
echo "$CMD" | grep -qE '\-\-no-verify' && exit 0

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$ROOT" 2>/dev/null || exit 0

STAGED=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null)
[[ -z "$STAGED" ]] && exit 0

# timeout portable: macOS no trae `timeout` (sí `gtimeout` con coreutils). Array vacío
# = correr sin wrapper (el timeout del hook en settings.json es el backstop).
if command -v timeout >/dev/null 2>&1; then TO=(timeout 60)
elif command -v gtimeout >/dev/null 2>&1; then TO=(gtimeout 60)
else TO=(); fi

BLOCKERS=""

# ── Dart / Flutter ──────────────────────────────────────────────────────────
DART_FILES=$(echo "$STAGED" | grep -E '\.dart$' || true)
if [[ -n "$DART_FILES" ]] && command -v dart >/dev/null 2>&1; then
    OUT=$(echo "$DART_FILES" | tr '\n' '\0' | xargs -0 "${TO[@]}" dart analyze 2>/dev/null)
    N=$(echo "$OUT" | grep -cE '^[[:space:]]+error ')
    [[ "$N" -gt 0 ]] && BLOCKERS+="dart analyze: ${N} error(es) en archivos staged. "
fi

# ── Python ──────────────────────────────────────────────────────────────────
PY_FILES=$(echo "$STAGED" | grep -E '\.py$' || true)
if [[ -n "$PY_FILES" ]] && command -v python3 >/dev/null 2>&1; then
    ERR=$(echo "$PY_FILES" | tr '\n' '\0' | xargs -0 "${TO[@]}" python3 -m py_compile 2>&1)
    [[ -n "$ERR" ]] && BLOCKERS+="py_compile: error(es) de sintaxis en archivos staged. "
fi

# ── Shell ─────────────────────────────────────────────────────────────────────
SH_FILES=$(echo "$STAGED" | grep -E '\.sh$' || true)
if [[ -n "$SH_FILES" ]]; then
    while IFS= read -r f; do
        [[ -f "$f" ]] || continue
        bash -n "$f" 2>/dev/null || BLOCKERS+="bash -n: error de sintaxis en ${f}. "
    done <<< "$SH_FILES"
fi

if [[ -n "$BLOCKERS" ]]; then
    printf '{"decision":"block","reason":"GATE MECÁNICO (2º auditor): %s Corrige los errores, o corre /second-auditor para el audit completo (mecánico + agente revisor independiente + tests). Override intencional: git commit --no-verify."}\n' "$BLOCKERS"
    exit 0
fi

# Stacks sin bloqueo automático fiable: recordar el gate completo (advisory, no bloquea).
if echo "$STAGED" | grep -qE '\.(ts|tsx|js|jsx|java|kt|rs|go|swift)$'; then
    echo "[2º AUDITOR] Commit con código no cubierto por el gate mecánico rápido. Antes de entregar/mergear, corre /second-auditor (typecheck/test + agente revisor independiente)." >&2
fi

# Advisory: si el diff es grande, sugerir /second-auditor
STAGED_LINES=$(git diff --cached --stat 2>/dev/null | tail -1 | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' || echo 0)
if [ "${STAGED_LINES:-0}" -gt 150 ]; then
    echo '{"type":"advisory","message":"⚠️ Diff grande (>150 líneas staged). Considera correr /second-auditor antes de commitear."}' >&2
fi
exit 0
