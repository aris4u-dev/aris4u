#!/usr/bin/env bash
# Runner canónico COMBINADO: suite principal (tests/) + árbol aislado console/tests/.
# Nace de la auditoría 2026-07-05: `pytest tests/` NO descubre console/tests/
# (200 tests, ~10% del total) porque viven en un árbol separado sin entrada en
# pyproject `testpaths`. Este runner los corre juntos sin tocar la config de la
# suite verde. Uso: bash run_all_tests.sh [args extra de pytest]
set -euo pipefail
cd "$(dirname "$0")"
PY="${PY:-.venv312/bin/python3}"

echo "=== tests/ (suite principal) ==="
"$PY" -m pytest tests/ "$@"

echo ""
echo "=== console/tests/ (árbol aislado) ==="
"$PY" -m pytest console/tests/ "$@"

echo ""
echo "OK: ambas suites pasaron"
