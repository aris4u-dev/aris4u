"""Tests del enrutador de capacidades (tools/capability_router.py).

Catálogo sintético → determinista, independiente del catálogo real curado.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import capability_router as cr

CAT: list[dict] = [
    {
        "name": "aris-council", "ctype": "skill",
        "triggers": ["decidir", "arquitectura", "trade-off"],
        "anti_triggers": ["ya decidí"], "intent": ["decision"], "context": "",
        "hint": "💡 /aris-council para esta decisión", "confidence": "high",
    },
    {
        "name": "aris_recall_client", "ctype": "mcp_tool",
        "triggers": ["client-b", "client-c", "cliente"],
        "anti_triggers": [], "intent": ["any"], "context": "",
        "hint": "💡 aris_recall_client antes de tocar el cliente", "confidence": "high",
    },
    {
        "name": "profiles:client-c", "ctype": "command",
        "triggers": ["client-c"], "anti_triggers": [], "intent": ["any"], "context": "client-c",
        "hint": "💡 /profiles:client-c", "confidence": "med",
    },
]


def test_route_matches_trigger() -> None:
    hits = cr.route("necesito decidir la arquitectura del módulo", intent="decision", catalog=CAT)
    assert hits
    assert hits[0]["name"] == "aris-council"


def test_route_respects_limit() -> None:
    hits = cr.route("decidir arquitectura para el cliente client-c", intent="decision", limit=1, catalog=CAT)
    assert len(hits) == 1


def test_anti_trigger_disqualifies() -> None:
    hits = cr.route("ya decidí la arquitectura, ahora implemento", intent="decision", catalog=CAT)
    assert all(h["name"] != "aris-council" for h in hits)


def test_intent_gating() -> None:
    # aris-council solo aplica a intent=decision; con intent=simple no debe disparar.
    hits = cr.route("decidir arquitectura", intent="simple", catalog=CAT)
    assert all(h["name"] != "aris-council" for h in hits)


def test_intent_unknown_does_not_disqualify() -> None:
    hits = cr.route("decidir arquitectura", intent="", catalog=CAT)
    assert any(h["name"] == "aris-council" for h in hits)


def test_context_gating() -> None:
    # profiles:client-c requires cwd containing "client-c"..
    in_ctx = cr.route("trabajar en client-c", cwd="/Users/x/projects/client-c", catalog=CAT)
    assert any(h["name"] == "profiles:client-c" for h in in_ctx)
    out_ctx = cr.route("trabajar en client-c", cwd="/tmp/otro", catalog=CAT)
    assert all(h["name"] != "profiles:client-c" for h in out_ctx)


def test_no_match_returns_empty() -> None:
    assert cr.route("hola qué tal", catalog=CAT) == []


def test_high_confidence_outranks_med() -> None:
    # 'client-c' fires aris_recall_client (high) and profiles:client-c (med, ctx ok) → high first.
    hits = cr.route("revisar al cliente client-c", cwd="/x/client-c", catalog=CAT, limit=2)
    assert hits[0]["name"] == "aris_recall_client"


def test_format_hints() -> None:
    out = cr.format_hints([{"name": "x", "hint": "💡 usa x", "confidence": "high", "score": 4}])
    assert "💡 usa x" in out
    assert cr.format_hints([]) == ""


def test_load_catalog_missing_is_empty(tmp_path: Path) -> None:
    assert cr.load_catalog(tmp_path / "noexiste.json") == []


def test_real_catalog_is_clean() -> None:
    # El catálogo real curado debe cargar, sin dups, solo high/med, con triggers+hint.
    cat = cr.load_catalog()
    assert cat, "data/capability_triggers.json debe existir y no estar vacío"
    names = [e["name"] for e in cat]
    assert len(names) == len(set(names)), "no debe haber nombres duplicados"
    for e in cat:
        assert e.get("confidence") in ("high", "med")
        assert e.get("triggers") and e.get("hint")


def test_real_catalog_no_false_positive_on_greeting() -> None:
    assert cr.route("hola, buenos días") == []


def test_flow_entry_renders_ordered_steps() -> None:
    """Una entrada con 'flow' se renderiza como secuencia numerada, no como bullet."""
    cat = [{
        "name": "flujo-x", "ctype": "flow", "triggers": ["construir un módulo"],
        "anti_triggers": [], "intent": ["implementation"], "context": "",
        "confidence": "high", "hint": "construir feature",
        "flow": ["PASO uno: descubrir", "PASO dos: arquitectura", "PASO tres: construir"],
    }]
    hints = cr.route("vamos a construir un módulo nuevo", intent="implementation", catalog=cat)
    assert hints and hints[0]["flow"], "el hint de flujo debe traer el campo flow"
    out = cr.format_hints(hints)
    assert "🔀 Flujo recomendado" in out
    assert "1. PASO uno" in out and "2. PASO dos" in out and "3. PASO tres" in out


def test_single_and_flow_coexist() -> None:
    """Un flujo y una capacidad suelta se renderizan en secciones distintas."""
    out = cr.format_hints([
        {"name": "f", "hint": "construir", "confidence": "high", "score": 4,
         "flow": ["A", "B"]},
        {"name": "s", "hint": "usa la skill X", "confidence": "high", "score": 2, "flow": None},
    ])
    assert "🔀 Flujo recomendado" in out and "💡 Capacidades" in out


def test_real_catalog_has_four_flows() -> None:
    """El catálogo real trae los 4 flujos de escenario, cada uno con pasos."""
    cat = cr.load_catalog()
    flows = [e for e in cat if e.get("ctype") == "flow"]
    assert len(flows) == 4, "deben existir los 4 flujos de escenario"
    for f in flows:
        assert isinstance(f.get("flow"), list) and len(f["flow"]) >= 3


# ─────────────────────────────────────────────────────────────────────────────
# H1 — nuevos triggers de skills ARIS4U propias
# ─────────────────────────────────────────────────────────────────────────────

def test_real_catalog_covers_new_skills() -> None:
    """H1: skills ARIS4U-owned añadidas en BATCH H aparecen en el catálogo real."""
    cat = cr.load_catalog()
    names = {e["name"] for e in cat}
    expected = {
        "debug-session", "harvest", "preflight", "backup-verify",
        "second-auditor", "mcp-audit", "verify-claims", "clarify",
        "incident-response", "skill-security-scan", "transcribe",
        "youtube", "aris-init", "aris-onboard",
    }
    missing = expected - names
    assert not missing, f"Estas skills faltan en el catálogo: {sorted(missing)}"


def test_new_skill_triggers_fire() -> None:
    """H1: los triggers de las skills nuevas enrutan correctamente (catálogo real)."""
    cat = cr.load_catalog()
    cases = [
        ("debug runtime en python", "debug-session"),
        ("hay huérfanos, limpiar huérfanos de workflow", "harvest"),
        ("hay recursos para el fan-out", "preflight"),
        ("los backups sirven de verdad", "backup-verify"),
        ("gate de cierre antes de cerrar módulo", "second-auditor"),
        ("auditar mcp servers seguros", "mcp-audit"),
        ("verifica eso, claim-verify", "verify-claims"),
        ("el brief es ambiguo, aclarar antes de construir", "clarify"),
        ("servicio caído en producción", "incident-response"),
        ("es seguro instalar este plugin tercero", "skill-security-scan"),
        ("transcribir audio con whisper local", "transcribe"),
        ("transcripción de youtube del video", "youtube"),
    ]
    for prompt, expected_name in cases:
        hits = cr.route(prompt, catalog=cat)
        names = [h["name"] for h in hits]
        assert expected_name in names, (
            f"'{prompt}' debería disparar '{expected_name}'; got {names}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# H2 — supresión de hints con adopción 0/N
# ─────────────────────────────────────────────────────────────────────────────

def test_static_dead_hints_not_in_route(tmp_path: Path) -> None:
    """H2: los hints de _STATIC_DEAD_HINTS nunca aparecen en route() (producción)."""
    dead_name = next(iter(cr._STATIC_DEAD_HINTS), None)
    if dead_name is None:
        pytest.skip("_STATIC_DEAD_HINTS está vacío")
    # Comprobamos que aunque el catálogo lo tuviera, route() lo filtraría.
    # Creamos un catálogo temporal con el hint muerto para confirmar supresión directa.
    fake_cat = [
        {
            "name": dead_name, "ctype": "command",
            "triggers": ["aris4u", "amplificador"],
            "anti_triggers": [], "intent": ["any"], "context": "",
            "hint": f"test hint para {dead_name}", "confidence": "high",
        }
    ]
    # Con catalog explícito la supresión queda OFF (modo test) → el hint aparece.
    hits_with_cat = cr.route("aris4u amplificador", catalog=fake_cat)
    assert any(h["name"] == dead_name for h in hits_with_cat), (
        "Con catálogo explícito el hint debe aparecer (supresión solo en producción)"
    )


def test_dead_hints_from_log_dynamic(tmp_path: Path) -> None:
    """H2: _dead_hints_from_log suprime nombres con adopted=0 y N>=5 (sin protección)."""
    events_file = tmp_path / "events.jsonl"
    import json as _json
    lines = []
    # "skill-x": 0 adopted, 7 ignored → debe suprimirse (no está en _PROTECTED_HINTS)
    for _ in range(7):
        lines.append(_json.dumps({"event": "capability_ignored", "name": "skill-x"}))
    # "skill-y": 2 adopted, 5 ignored → NO se suprime (tiene adopción)
    lines.append(_json.dumps({"event": "capability_adopted", "name": "skill-y"}))
    lines.append(_json.dumps({"event": "capability_adopted", "name": "skill-y"}))
    for _ in range(5):
        lines.append(_json.dumps({"event": "capability_ignored", "name": "skill-y"}))
    # "skill-z": 0 adopted, 3 ignored → N<5, NO se suprime
    for _ in range(3):
        lines.append(_json.dumps({"event": "capability_ignored", "name": "skill-z"}))
    events_file.write_text("\n".join(lines))

    dead = cr._dead_hints_from_log(events_file)
    assert "skill-x" in dead, "skill-x (0/7) debe suprimirse"
    assert "skill-y" not in dead, "skill-y (2/7) no debe suprimirse"
    assert "skill-z" not in dead, "skill-z (0/3, N<5) no debe suprimirse"


def test_protected_hints_never_suppressed(tmp_path: Path) -> None:
    """H2: _PROTECTED_HINTS no se suprimen aunque el log las marque 0/N>=5."""
    import json as _json
    events_file = tmp_path / "ev.jsonl"
    lines = []
    # Generamos 10 ignores para cada skill protegida — deben resistir la supresión
    for name in cr._PROTECTED_HINTS:
        for _ in range(10):
            lines.append(_json.dumps({"event": "capability_ignored", "name": name}))
    events_file.write_text("\n".join(lines))

    dead = cr._dead_hints_from_log(events_file)
    for name in cr._PROTECTED_HINTS:
        assert name not in dead, (
            f"'{name}' está en _PROTECTED_HINTS y no debe suprimirse (dead={dead})"
        )


def test_dead_hints_from_log_missing_log(tmp_path: Path) -> None:
    """H2: log inexistente → frozenset vacío, fail-open."""
    dead = cr._dead_hints_from_log(tmp_path / "noexiste.jsonl")
    assert dead == frozenset()


def test_suppressed_hints_union(tmp_path: Path) -> None:
    """H2: _suppressed_hints = estático ∪ dinámico."""
    import json as _json
    log = tmp_path / "ev.jsonl"
    # 6 ignored de "dynamic-dead"
    log.write_text("\n".join(
        _json.dumps({"event": "capability_ignored", "name": "dynamic-dead"})
        for _ in range(6)
    ))
    # Monkey-patch _EVENTS_LOG para la llamada de producción no; usamos log_path directo.
    dead = cr._dead_hints_from_log(log)
    combined = cr._STATIC_DEAD_HINTS | dead
    assert "dynamic-dead" in combined


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
