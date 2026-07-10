#!/bin/bash
# backup_to_w2.sh — backup VERIFICADO de la memoria de ARIS4U a W2 (off-machine).
#
# FREEZE 4 semanas (ítem 2, §7 del MASTER) · defensa del modo de fallo #4 ("backup
# teatro": un cliente generó .sql.gz de 20 bytes durante semanas; un backup que nunca
# se restaura NO existe). Por eso este script SIEMPRE restaura a /tmp y cuenta filas
# antes de declarar éxito.
#
# Diseño (regla A3 del pre-mortem): NUNCA se respalda un .db caliente con cp. Primero
# `sqlite3 .backup` (snapshot consistente que respeta el WAL) a un staging, y restic
# respalda el staging. Backend = SFTP sobre la conexión ssh `w2` ya existente; restic
# corre solo en el Mac (W2 únicamente recibe por sftp). Password = macOS Keychain.
#
# Uso:  bash tools/backup_to_w2.sh           (backup + verify)
#        bash tools/backup_to_w2.sh init     (inicializa el repo restic una sola vez)
set -o pipefail

export PATH="/opt/homebrew/bin:$PATH"
ARIS4U_ROOT="${ARIS4U_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG="$ARIS4U_ROOT/logs/backup.jsonl"
STAGING="$(mktemp -d /tmp/aris4u_backup.XXXXXX)"
VERIFY_DIR="$(mktemp -d /tmp/aris4u_verify.XXXXXX)"
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# Repo restic vía SFTP a W2. Password desde Keychain (secreto crítico, no en disco).
export RESTIC_REPOSITORY="sftp:w2:/home/YOUR_USERNAME/aris4u-backups"
RESTIC_PASSWORD="$(security find-generic-password -s 'aris4u-restic-repo' -a "$USER" -w 2>/dev/null)"
export RESTIC_PASSWORD

_log() { printf '%s\n' "$1" >> "$LOG" 2>/dev/null || true; }
_cleanup() { rm -rf "$STAGING" "$VERIFY_DIR"; }
trap _cleanup EXIT

mkdir -p "$ARIS4U_ROOT/logs" 2>/dev/null

if [ -z "$RESTIC_PASSWORD" ]; then
    _log "{\"ts\":\"$TS\",\"event\":\"backup\",\"ok\":false,\"error\":\"no_keychain_password\"}"
    echo "ERROR: sin password en Keychain (aris4u-restic-repo)" >&2
    exit 1
fi

# init: inicializa el repo una sola vez (idempotente: no falla si ya existe).
if [ "$1" = "init" ]; then
    if restic snapshots >/dev/null 2>&1; then
        echo "repo restic ya inicializado"
    else
        restic init && echo "repo restic inicializado en $RESTIC_REPOSITORY"
    fi
    exit $?
fi

# 1. Snapshot consistente de cada DB con sqlite3 .backup (jamás cp de .db caliente).
DBS=(
    "$ARIS4U_ROOT/data/sessions.db"
    "$ARIS4U_ROOT/data/aris_vectors.db"
    "$HOME/.claude-mem/claude-mem.db"
)
for db in "${DBS[@]}"; do
    [ -f "$db" ] || continue
    sqlite3 "$db" ".backup '$STAGING/$(basename "$db")'" 2>/dev/null || {
        _log "{\"ts\":\"$TS\",\"event\":\"backup\",\"ok\":false,\"error\":\"sqlite_backup_failed:$(basename "$db")\"}"
        echo "ERROR: sqlite3 .backup falló para $db" >&2
        exit 1
    }
done

# 2. restic backup del staging consistente.
if ! restic backup "$STAGING" --tag aris4u-memory --host aris4u-mac >/dev/null 2>&1; then
    _log "{\"ts\":\"$TS\",\"event\":\"backup\",\"ok\":false,\"error\":\"restic_backup_failed\"}"
    echo "ERROR: restic backup falló" >&2
    exit 1
fi

# 3. VERIFICAR restaurando el último snapshot a /tmp + COUNT(decisions) > umbral.
#    Un backup sin restore verificado = no existe.
if ! restic restore latest --target "$VERIFY_DIR" >/dev/null 2>&1; then
    _log "{\"ts\":\"$TS\",\"event\":\"backup\",\"ok\":false,\"error\":\"restore_failed\"}"
    echo "ERROR: restore de verificación falló" >&2
    exit 1
fi
RESTORED_DB=$(find "$VERIFY_DIR" -name sessions.db | head -1)
COUNT=$(sqlite3 "$RESTORED_DB" "SELECT COUNT(*) FROM decisions" 2>/dev/null || echo 0)
THRESHOLD=50  # piso de cordura; hoy hay 231 decisions
if [ "$COUNT" -lt "$THRESHOLD" ]; then
    _log "{\"ts\":\"$TS\",\"event\":\"backup\",\"ok\":false,\"error\":\"restore_count_below_threshold\",\"decisions\":$COUNT}"
    echo "ERROR: restore verificado trajo solo $COUNT decisions (< $THRESHOLD)" >&2
    exit 1
fi

# 4. Retención: 7 diarios + 4 semanales, prune del resto.
restic forget --keep-daily 7 --keep-weekly 4 --prune >/dev/null 2>&1 || true

_log "{\"ts\":\"$TS\",\"event\":\"backup\",\"ok\":true,\"decisions_verified\":$COUNT}"
echo "backup OK — restore verificado: $COUNT decisions"
exit 0
