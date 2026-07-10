#!/bin/bash
# write_client_bridge.sh — escribe el puente de cliente que el demonio MCP lee.
#
# El servidor MCP de ARIS4U es un proceso de larga vida con cwd neutro (~), así que
# NO puede detectar el cliente activo por su propio cwd. Este puente — escrito por los
# hooks que SÍ ven el cwd de la sesión (depth_inject en UserPromptSubmit,
# lab_session_init en SessionStart) — se lo comunica. session_manager._client_from_session_bridge()
# lo lee con TTL de 1h. Bash puro (sin Python) para no erosionar el presupuesto <100ms.
#
# Uso: write_client_bridge.sh <cwd-de-la-sesión>
CWD="${1:-$PWD}"
CLIENT=""
if [[ "$CWD" =~ /projects/03-clients/([^/]+) ]]; then
    CLIENT="${BASH_REMATCH[1]}"              # nombre de carpeta completo (genérico: /projects/03-clients/<cliente>)
fi
CLIENT=$(printf '%s' "$CLIENT" | tr '[:upper:]' '[:lower:]')  # canónico lower-case
# Quitar sufijo conocido (mi-plataforma→mi-cliente). NUNCA %%-* (bug acme-wellness→'acme').
for _suf in -platform -website -app -web; do
    CLIENT="${CLIENT%$_suf}"
done
# Puente POR-SESIÓN (fix P0 cross-client leak): cada sesión escribe SU archivo, indexado
# por CLAUDE_CODE_SESSION_ID. El daemon MCP hereda esa misma variable y lee exactamente
# este archivo, así dos sesiones en clientes distintos NO se pisan.
SID="${CLAUDE_CODE_SESSION_ID:-}"
if [ -n "$SID" ]; then
    BRIDGE="/tmp/aris4u_active_client.${SID}.json"
else
    BRIDGE="/tmp/aris4u_active_client.json"  # fallback: ejecución sin session id (tests/manual)
fi
printf '{"client_id":"%s","cwd":"%s","ts":%s}\n' "$CLIENT" "$CWD" "$(date +%s)" \
    > "$BRIDGE" 2>/dev/null || true
# limpieza best-effort de puentes de sesiones viejas (>1 día)
find /tmp -maxdepth 1 -name 'aris4u_active_client.*.json' -mtime +1 -delete 2>/dev/null || true
