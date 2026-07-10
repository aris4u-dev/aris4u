#!/bin/bash
# pre-bash-guard.sh — PreToolUse BLOQUEANTE consolidado para comandos Bash.
# Combina: credential-scan + PHI-guard + cluster-impact.
#
# Portado desde ~/.claude/hooks/pre-bash-guard.sh para distribución como
# parte del plugin ARIS4U. Fuente versionada: hooks/standalone/ (plugin repo).
#
# PORTABILIDAD:
#   - Usa $HOME (nunca paths hardcodeados de usuario).
#   - Todas las dependencias son estándar: bash, python3, grep, git.
#   - PHI_SAFE_DESTINATIONS: lista de destinos considerados seguros para egreso
#     de datos sensibles. Override vía env: PHI_SAFE_DESTINATIONS="host1 host2"
#     (espacio como separador). Default incluye "localhost" y "127.0.0.1"; la
#     lista puede extenderse con hostnames de tu cluster local.
#   - FAIL-OPEN: ante errores de parseo siempre sale exit 0.
#
# Salida: exit 2 = BLOQUEA (stderr = razón). exit 0 = permite.

INPUT=$(cat)

# JSON parse — command-substitution preserva el comando íntegro (incl. espacios/saltos)
# y elimina el riesgo de inyección de eval.
# Protocolo: print(tool_name) en línea 1, sys.stdout.write(command) en el resto.
_parsed=$(printf '%s' "$INPUT" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('tool_name', '') or '')
    sys.stdout.write(d.get('tool_input', {}).get('command', '') or '')
except Exception:
    print('')
" 2>/dev/null)
TOOL="${_parsed%%$'\n'*}"
CMD="${_parsed#*$'\n'}"

[[ "$TOOL" != "Bash" ]] && exit 0
[[ -z "$CMD" ]] && exit 0

# 1. CREDENTIAL SCAN (git commit only)
if echo "$CMD" | grep -qE '^git\s+commit'; then
    STAGED=$(git diff --cached --diff-filter=ACM 2>/dev/null)
    if [[ -n "$STAGED" ]]; then
        FOUND=""
        echo "$STAGED" | grep -qEi '(api[_-]?key|apikey)\s*[:=]\s*["'"'"'][A-Za-z0-9_\-]{20,}' && FOUND+="API key. "
        echo "$STAGED" | grep -qE 'AKIA[0-9A-Z]{16}' && FOUND+="AWS key. "
        echo "$STAGED" | grep -qEi '(secret|password|passwd|token|credential)\s*[:=]\s*["'"'"'][^\s]{8,}' && FOUND+="Secret/token. "
        echo "$STAGED" | grep -q 'BEGIN.*PRIVATE KEY' && FOUND+="Private key. "
        echo "$STAGED" | grep -qEi '(mongodb|postgres|mysql|redis)://[^:]+:[^@]+@' && FOUND+="DB connstring. "
        echo "$STAGED" | grep -qE '(sk-ant-|sk-[a-zA-Z0-9]{40,}|sk-proj-)' && FOUND+="AI API key. "
        if [[ -n "$FOUND" ]]; then
            printf '{"decision": "block", "reason": "CREDENTIAL SCAN: %sRemove secrets before committing."}\n' "$FOUND"
            exit 0
        fi
    fi
fi

# 2. PHI GUARD (all Bash commands)
# Bloquea SOLO si el comando EGRESA a una red/API externa (no falsos positivos
# en grep/git/sed/cat/docker con términos PHI en el propio comando).
input_lower=$(echo "$CMD" | tr '[:upper:]' '[:lower:]')
has_phi=false
for p in "patient" "paciente" "social.security" "date.of.birth" "fecha.de.nacimiento" \
    "medical.record" "historial.medico" "historia.clinica" "diagnosis" "diagnostico" \
    "treatment.plan" "insurance.id" "medicare" "medicaid" "protected.health" \
    "prescription" "medication" "lab.result" "blood.type" "vital.sign" \
    "run.report" "dispatch.*unit" "ambulance.*crew" "transport.*patient"; do
    if echo "$input_lower" | grep -qiE "$p"; then
        has_phi=true; break
    fi
done

if [ "$has_phi" = true ]; then
    is_egress=false
    echo "$CMD" | grep -qiE '\b(curl|wget|nc|telnet|ftp|scp|rsync|httpie)\b' && is_egress=true
    echo "$CMD" | grep -qiE 'https?://' && is_egress=true
    if [ "$is_egress" = true ]; then
        # Destinos seguros: configurable vía env PHI_SAFE_DESTINATIONS
        # Default: localhost, 127.0.0.1, y herramientas locales comunes
        SAFE_DESTS="${PHI_SAFE_DESTINATIONS:-localhost 127.0.0.1 ollama 11434}"
        is_safe=false
        for safe in $SAFE_DESTS; do
            if echo "$CMD" | grep -qi "$safe"; then
                is_safe=true; break
            fi
        done
        if [ "$is_safe" = false ]; then
            echo "BLOCKED: PHI/patient data hacia un destino EXTERNO. Procesa local (Ollama/instancia local)." >&2
            exit 2
        fi
    fi
fi

# 3. CLUSTER IMPACT (warnings only — advisory, nunca bloquea)
W=""
echo "$CMD" | grep -qE '(apt|apt-get)\s+(install|upgrade|remove|purge)' && W+="[PKG] apt operation
"
echo "$CMD" | grep -qE 'pip3?\s+install.*-g|npm\s+install\s+-g' && W+="[PKG] Global install
"
echo "$CMD" | grep -qE 'docker\s+run\s' && {
    echo "$CMD" | grep -qE '(\-\-memory|\-\-cpus|mem_limit)' || W+="[DOCKER] No resource limits
"
}
echo "$CMD" | grep -qE 'docker\s+(system|volume|image)\s+prune' && W+="[DOCKER] Prune — dry-run first
"
echo "$CMD" | grep -qE 'systemctl\s+(enable|disable|start|stop|restart)\s' && W+="[SERVICE] systemctl change
"
echo "$CMD" | grep -qE 'ollama\s+(pull|rm|create)\s' && W+="[OLLAMA] Model change
"
echo "$CMD" | grep -qE 'rm\s+(-rf|-r)\s+(/|~/|/home|/etc|/var)' && W+="[DESTRUCTIVE] Recursive delete
"
echo "$CMD" | grep -qE 'mkfs|fdisk|dd\s+if=|wipefs' && W+="[DESTRUCTIVE] Disk operation
"

# SSH wrapper — detecta host remoto y avisa de operaciones de impacto
if echo "$CMD" | grep -qE '^ssh\s+\w+'; then
    target=$(echo "$CMD" | grep -oE 'ssh\s+\w+' | awk '{print $2}')
    remote_cmd=$(echo "$CMD" | sed "s/^ssh[[:space:]]\+[^ ]*[[:space:]]//")
    echo "$remote_cmd" | grep -qE 'systemctl\s+(start|stop|restart)' && W+="[REMOTE:$target] Service change
"
    echo "$remote_cmd" | grep -qE 'ollama\s+(pull|rm|create)' && W+="[REMOTE:$target] Ollama change
"
    echo "$remote_cmd" | grep -qE 'rm\s+(-rf|-r)\s+(/)' && W+="[REMOTE:$target] DESTRUCTIVE
"
fi

[[ -n "$W" ]] && echo -e "[CLUSTER IMPACT]\n$W"
exit 0
