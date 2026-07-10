#!/bin/bash
# start_validation_session.sh — inicia sesión con logging ARIS4U V16.1 activo
# Uso: source start_validation_session.sh
# O:   bash start_validation_session.sh (ejecuta y exporta vars)

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_DIR="${ARIS4U_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}/logs/${ARIS4U_VALIDATION_PROJECT:-validation}"
LOG_FILE="${LOG_DIR}/session_${TIMESTAMP}.jsonl"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"

export ARIS4U_VALIDATION_LOG=1
export ARIS4U_LOG_FILE="$LOG_FILE"

cat <<EOF
======================================
ARIS4U V16.1 Validation Session Started
======================================
Timestamp: $TIMESTAMP
Log File:  $LOG_FILE

Logging ACTIVE. Events captured:
  - depth_inject (latency, intent, recall hits, goal drift)
  - f5_prevalidation (result, latency, file)
  - novelty_detection (new domain, confidence)
  - autotest (tests_run, passed, failed)
  - depth_validator (research enforcement)
  - contract_guard (block/allow)
  - voting (models, decisions)
  - iteration_cap (escalation)
  - goal_checkpoint (preserved)
  - pre_compact/post_compact (state)
  - session_end (stats)

Zero overhead when env var is NOT set.

To analyze later:
  python3 "$ARIS4U_ROOT"/tools/analyze_validation_log.py $LOG_FILE

======================================
EOF

if [[ "$1" == "--echo" ]]; then
    echo "ARIS4U_VALIDATION_LOG=$ARIS4U_VALIDATION_LOG"
    echo "ARIS4U_LOG_FILE=$ARIS4U_LOG_FILE"
fi
