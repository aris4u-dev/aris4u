"""Tests de la capa semántica del enrutador (tools/capability_semantic.py).

Deterministas y SIN dependencia de Ollama: el embedder y la caché se mockean. Prueban la
construcción de records, el hash, el gating, la dedup y —lo más importante— el FAIL-OPEN.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from tools import capability_router as cr
from tools import capability_semantic as cs

# Catálogo curado sintético: una entrada con gating, una flow (debe ignorarse).
CATALOG: list[dict[str, Any]] = [
    {
        "name": "aris-council", "ctype": "skill",
        "triggers": ["decidir", "arquitectura"], "anti_triggers": ["ya decidí"],
        "intent": ["decision"], "context": "", "confidence": "high",
        "hint": "💡 /aris-council para decidir",
    },
    {
        "name": "flujo-x", "ctype": "flow", "triggers": ["construir"],
        "anti_triggers": [], "intent": ["implementation"], "context": "",
        "confidence": "high", "hint": "construir feature", "flow": ["A", "B", "C"],
    },
]

# Inventario sintético: una cap curada (aris-council), una solo-inventario, un hook (no rutable).
INVENTORY: list[dict[str, Any]] = [
    {"name": "aris-council", "ctype": "skill", "description": "consejo de razonamiento",
     "invocation": "/aris-council"},
    {"name": "simplify", "ctype": "skill", "description": "simplifica el código cambiado",
     "invocation": "/simplify"},
    {"name": "PreToolUse", "ctype": "hook", "description": "hook automático", "invocation": "auto"},
    {"name": "flujo-x", "ctype": "skill", "description": "no debería heredar el flow",
     "invocation": "/flujo-x"},
]


def test_build_routing_records_inherits_and_neutral() -> None:
    recs = cs.build_routing_records(CATALOG, INVENTORY)
    by = {r["name"]: r for r in recs}
    # hook NO rutable → ausente
    assert "PreToolUse" not in by
    # cap curada hereda intent/anti/hint del catálogo
    assert by["aris-council"]["intent"] == ["decision"]
    assert by["aris-council"]["anti_triggers"] == ["ya decidí"]
    assert by["aris-council"]["hint"] == "💡 /aris-council para decidir"
    # cap solo-inventario → gating neutro + hint sintetizado
    assert by["simplify"]["intent"] == ["any"]
    assert by["simplify"]["anti_triggers"] == []
    assert "simplify" in by["simplify"]["hint"]


def test_flow_entry_not_inherited_as_gating() -> None:
    # 'flujo-x' es flow en el catálogo → NO debe aportar gating; la cap homónima del
    # inventario queda como solo-inventario (neutral), nunca como flujo.
    recs = cs.build_routing_records(CATALOG, INVENTORY)
    fx = next(r for r in recs if r["name"] == "flujo-x")
    assert fx["intent"] == ["any"] and "flow" not in fx


def test_synth_hint_format() -> None:
    h = cs._synth_hint("simplify", "skill", "limpia el código", "/simplify")
    assert h.startswith("💡 simplify (skill)") and "/simplify" in h


def test_docs_hash_changes_with_content() -> None:
    r1 = cs.build_routing_records(CATALOG, INVENTORY)
    h1 = cs._docs_hash(r1)
    r2 = cs.build_routing_records(CATALOG, INVENTORY + [
        {"name": "z", "ctype": "skill", "description": "nueva", "invocation": "/z"}])
    assert h1 != cs._docs_hash(r2)


# ----------------------------- FAIL-OPEN ----------------------------- #
def test_augment_failopen_no_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "load_index", lambda: None)
    monkeypatch.setattr(cs, "_load_sidecar", lambda: None)
    assert cs.augment("audita esto", intent="fix") == []


def test_augment_failopen_embedder_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "_load_sidecar",
                        lambda: {"hash": "H", "records": [{"name": "a", "intent": ["any"]}]})
    monkeypatch.setattr(cs, "load_index",
                        lambda: {"matrix": np.array([[1.0, 0.0]], dtype=np.float32), "hash": "H"})
    monkeypatch.setattr(cs, "embed_text", lambda *a, **k: None)  # Ollama caído
    assert cs.augment("audita esto", intent="fix") == []


def test_augment_failopen_hash_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "_load_sidecar", lambda: {"hash": "H1", "records": []})
    monkeypatch.setattr(cs, "load_index",
                        lambda: {"matrix": np.array([[1.0]], dtype=np.float32), "hash": "H2"})
    assert cs.augment("audita esto") == []


def _mock_index(monkeypatch: pytest.MonkeyPatch, records: list[dict], query_vec: list[float]) -> None:
    """Mockea caché alineada + embedder determinista (todas las filas == query → sim 1.0)."""
    n = len(records)
    mat = np.tile(np.array(query_vec, dtype=np.float32), (n, 1))
    monkeypatch.setattr(cs, "_load_sidecar", lambda: {"hash": "H", "records": records})
    monkeypatch.setattr(cs, "load_index", lambda: {"matrix": mat, "hash": "H"})
    monkeypatch.setattr(cs, "embed_text", lambda *a, **k: list(query_vec))


def test_augment_gating_and_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [
        {"name": "a", "intent": ["any"], "anti_triggers": [], "context": "", "hint": "ha", "confidence": "med"},
        {"name": "b", "intent": ["decision"], "anti_triggers": [], "context": "", "hint": "hb", "confidence": "med"},
        {"name": "c", "intent": ["any"], "anti_triggers": [], "context": "", "hint": "hc", "confidence": "med"},
    ]
    _mock_index(monkeypatch, records, [1.0, 0.0, 0.0])
    # intent=fix → 'b' (solo decision) se filtra; exclude 'a' (ya cubierto por keyword) → queda 'c'.
    out = cs.augment("hacer la cosa", intent="fix", exclude_names={"a"})
    assert [h["name"] for h in out] == ["c"]
    assert out[0]["via"] == "semantic" and out[0]["sim"] == 1.0


def test_augment_threshold_excludes_low_sim(monkeypatch: pytest.MonkeyPatch) -> None:
    records = [{"name": "a", "intent": ["any"], "anti_triggers": [], "context": "", "hint": "ha", "confidence": "med"}]
    # matriz ortogonal al query → sim 0 < umbral → nada.
    monkeypatch.setattr(cs, "_load_sidecar", lambda: {"hash": "H", "records": records})
    monkeypatch.setattr(cs, "load_index",
                        lambda: {"matrix": np.array([[0.0, 1.0]], dtype=np.float32), "hash": "H"})
    monkeypatch.setattr(cs, "embed_text", lambda *a, **k: [1.0, 0.0])
    assert cs.augment("lo que sea") == []


# ----------------------- integración con route() ----------------------- #
def test_route_explicit_catalog_skips_semantic(monkeypatch: pytest.MonkeyPatch) -> None:
    # Con catálogo explícito (tests) la capa semántica NO se invoca (determinismo).
    called = {"n": 0}

    def boom(*a: Any, **k: Any) -> list:
        called["n"] += 1
        return []

    monkeypatch.setattr(cs, "augment", boom)
    cr.route("decidir arquitectura", intent="decision", catalog=CATALOG)
    assert called["n"] == 0


def test_route_failopen_when_semantic_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Si la capa semántica revienta, route() devuelve solo keyword (nunca propaga).
    def explode(*a: Any, **k: Any) -> list:
        raise RuntimeError("boom")

    monkeypatch.setattr(cs, "augment", explode)
    out = cr.route("hola qué tal", semantic=True)  # catalog real, sin keyword match
    assert out == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
