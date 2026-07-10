"""Tests del gate de contrato (pre-flight de auto-adaptación)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import contract_gate  # noqa: E402

# NOTA: el roundtrip real (check_memory) y la ruta local (check_local_route) son
# environment-coupled (DB real + Ollama) → se verifican corriendo la herramienta, no en
# la suite (el conftest redirige sessions.db a un tmp y rompería el smoke hardcodeado).


def test_guard_selector_is_keyword_expression() -> None:
    """El selector de guards es una expresión -k de keywords (sin nodeids con ruta)."""
    assert "::" not in contract_gate._GUARD_TESTS
    assert "/" not in contract_gate._GUARD_TESTS
    assert "test_migration_bad_blocks_exit2" in contract_gate._GUARD_TESTS
    assert "test_phi_healthcare_external_blocks_exit2" in contract_gate._GUARD_TESTS
