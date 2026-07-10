"""Tests del protocolo de orquestación (Fase 3).

Cubre: graduación por intención (simple→nada, fix→ligera, decision/research/
implementation→ciclo completo), referencia al inventario VIVO (nombres filtrados),
genericidad (toolkit de un tercero, cero nombres de cliente) y fail-open (snapshot
ausente/corrupto → "").
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import orchestration_protocol as op

# Inventario de muestra con el formato del snapshot VIVO (listas por tipo + mcp_tools).
_SAMPLE_SNAPSHOT: dict[str, object] = {
    "skills": ["aris-council", "clarify", "discover", "second-auditor",
               "code-review", "verify-claims", "enterprise-build",
               "feature-dev:feature-dev"],
    "agents": ["feature-dev:code-architect", "feature-dev:code-explorer"],
    "mcp_tools": {
        "aris4u": ["aris_recall_client", "aris_search", "aris_dialectic"],
    },
}


def _names() -> set[str]:
    return op.available_capability_names(snapshot=_SAMPLE_SNAPSHOT)


# --------------------------------------------------------------------------- #
# available_capability_names — lectura del inventario vivo
# --------------------------------------------------------------------------- #
def test_names_from_live_snapshot_format() -> None:
    names = _names()
    assert "aris-council" in names
    assert "aris_recall_client" in names  # de mcp_tools dict
    assert "feature-dev:code-architect" in names  # de agents


def test_names_from_unified_collect_format() -> None:
    unified = {"capabilities": [{"name": "aris-council", "ctype": "skill"},
                                {"name": "second-auditor", "ctype": "skill"}]}
    names = op.available_capability_names(snapshot=unified)
    assert names == {"aris-council", "second-auditor"}


def test_names_missing_snapshot_failopen(tmp_path: Path) -> None:
    assert op.available_capability_names(snapshot_path=tmp_path / "nope.json") == set()


def test_names_corrupt_snapshot_failopen(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    assert op.available_capability_names(snapshot_path=p) == set()


# --------------------------------------------------------------------------- #
# build_protocol — graduación por intención
# --------------------------------------------------------------------------- #
def test_simple_intent_no_protocol() -> None:
    assert op.build_protocol("simple", _names()) == ""


def test_unknown_intent_no_protocol() -> None:
    assert op.build_protocol("", _names()) == ""
    assert op.build_protocol("garbage", _names()) == ""


@pytest.mark.parametrize("intent", ["decision", "research", "implementation"])
def test_full_protocol_has_ordered_cycle(intent: str) -> None:
    block = op.build_protocol(intent, _names())
    assert block
    # Las 4 fases en ORDEN.
    for i, phase in enumerate(["ENTENDER", "DISEÑAR", "CONSTRUIR", "VERIFICAR"]):
        assert f"{i + 1}. {phase}" in block
    # Orden posicional estricto.
    assert (
        block.index("ENTENDER")
        < block.index("DISEÑAR")
        < block.index("CONSTRUIR")
        < block.index("VERIFICAR")
    )
    assert f"intent={intent}" in block


def test_full_protocol_names_live_capabilities() -> None:
    block = op.build_protocol("implementation", _names())
    # Capacidades reales del inventario, una por fase.
    assert "aris_recall_client" in block  # ENTENDER
    assert "aris-council" in block        # DISEÑAR
    assert "feature-dev:feature-dev" in block  # CONSTRUIR
    assert "second-auditor" in block      # VERIFICAR
    # Instrucción genérica "usa la capacidad correcta de tu inventario".
    assert "inventario" in block.lower()


def test_fix_intent_light_protocol() -> None:
    block = op.build_protocol("fix", _names())
    assert block.startswith("🧭 ORQUESTA (fix)")
    assert "ENTENDER" in block and "VERIFICAR" in block
    # La ligera NO despliega las 4 fases numeradas.
    assert "1. ENTENDER" not in block
    assert len(block) < len(op.build_protocol("implementation", _names()))


def test_empty_inventory_failopen() -> None:
    assert op.build_protocol("implementation", set()) == ""
    assert op.build_protocol("fix", set()) == ""


# --------------------------------------------------------------------------- #
# Genericidad — toolkit de un tercero + cero nombres de cliente
# --------------------------------------------------------------------------- #
def test_generic_third_party_toolkit() -> None:
    """Un tercero con OTRO toolkit: las fases sin capacidad usan guía genérica."""
    third = {"skills": ["enterprise-build"], "agents": [], "mcp_tools": {}}
    names = op.available_capability_names(snapshot=third)
    block = op.build_protocol("implementation", names)
    assert block  # el ciclo se inyecta aunque el toolkit difiera
    # CONSTRUIR tiene enterprise-build; las otras fases caen a guía genérica.
    assert "enterprise-build" in block
    assert "cap de inventario para entender" in block.lower()
    assert "cap de inventario para diseñar" in block.lower()


def test_no_client_names_anywhere() -> None:
    """El protocolo NUNCA contiene nombres de cliente (genérico)."""
    blob = " ".join(
        [
            op.build_protocol("implementation", _names()),
            op.build_protocol("fix", _names()),
            op.build_session_posture(_names()),
        ]
    ).lower()
    for client in ("client-b", "client-c", "client-a", "client-d", "lab-project-1", "client-d-co", "client-e"):
        assert client not in blob


# --------------------------------------------------------------------------- #
# build_session_posture
# --------------------------------------------------------------------------- #
def test_session_posture_present_with_inventory() -> None:
    p = op.build_session_posture(_names())
    assert "POSTURA" in p
    for phase in ("ENTENDER", "DISEÑAR", "CONSTRUIR", "VERIFICAR"):
        assert phase in p


def test_session_posture_empty_inventory_failopen() -> None:
    assert op.build_session_posture(set()) == ""


# --------------------------------------------------------------------------- #
# Presupuesto de chars (anti-bloat)
# --------------------------------------------------------------------------- #
def test_protocol_within_char_budget() -> None:
    for intent in ("decision", "research", "implementation", "fix"):
        block = op.build_protocol(intent, _names())
        assert len(block) <= op.MAX_PROTOCOL_CHARS
    assert len(op.build_session_posture(_names())) < 600


def test_cli_smoke(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # CLI no debe lanzar; usa el snapshot real del repo (puede estar vacío → degrada).
    rc = op.main(["implementation"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "inventario vivo" in out
    json.dumps(out)  # smoke: output serializable


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
