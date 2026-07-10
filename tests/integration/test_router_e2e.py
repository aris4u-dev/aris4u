"""Test de integración END-TO-END del router de capacidades.

Invoca el hook UserPromptSubmit REAL (vía dispatch.py, subprocess) con prompts sintéticos
y verifica que el flujo de escenario aparece en el additionalContext emitido.

Existe porque un unit test de route() pasaba PERO el router nunca inyectaba en producción:
dispatch.py no ponía el root de aris4u en sys.path → `import tools.capability_router`
fallaba → el except fail-open lo tragaba en silencio. Solo una prueba end-to-end (no de la
función aislada) lo caza. NO borrar: es la red contra ese bug latente.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ARIS = Path(__file__).resolve().parent.parent.parent
PY = ARIS / ".venv312" / "bin" / "python3"
DISPATCH = ARIS / "hooks" / "dispatch.py"


def _run_hook(prompt: str) -> str:
    """Invoca el hook real y devuelve el additionalContext emitido ('' si nada)."""
    inp = json.dumps({
        "session_id": "pytest-e2e", "cwd": str(ARIS),
        "hook_event_name": "UserPromptSubmit", "prompt": prompt,
    })
    proc = subprocess.run(
        [str(PY), str(DISPATCH), "UserPromptSubmit"],
        input=inp, capture_output=True, text=True, timeout=60,
    )
    out = (proc.stdout or "").strip()
    if not out:
        return ""
    try:
        return json.loads(out).get("additionalContext", "")
    except json.JSONDecodeError:
        return out


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
@pytest.mark.parametrize("prompt,expected_flow", [
    ("vamos a construir un modulo nuevo de inventario", "construir feature"),
    ("quiero instalar una skill de github", "evaluar/instalar herramienta"),
    ("necesito trabajar en el crm de un cliente", "trabajar/auditar un cliente"),
    ("cual es mejor framework decidir entre dos", "research / decisión"),
])
def test_scenario_injects_its_flow(prompt: str, expected_flow: str) -> None:
    """Cada escenario de trabajo inyecta su flujo ordenado en el additionalContext."""
    ctx = _run_hook(prompt)
    assert "🔀 Flujo recomendado" in ctx, f"no se inyectó NINGÚN flujo para: {prompt!r}"
    assert expected_flow in ctx, f"se inyectó un flujo, pero no '{expected_flow}' para {prompt!r}"


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
@pytest.mark.parametrize("prompt", [
    "hola buenos dias",
    "arreglar el bug del error en login",
    "que hora es",
])
def test_non_scenario_does_not_inject_flow(prompt: str) -> None:
    """Saludos, fixes y triviales NO disparan un flujo (precisión: sin falsos positivos)."""
    ctx = _run_hook(prompt)
    assert "🔀 Flujo recomendado" not in ctx, f"falso positivo: inyectó flujo para {prompt!r}"


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
def test_steps_are_present_and_ordered() -> None:
    """El flujo construir trae sus pasos numerados (no solo el título)."""
    ctx = _run_hook("vamos a construir un modulo nuevo")
    assert "1. DESCUBRIR" in ctx and "2. ARQUITECTURA" in ctx, "faltan los pasos numerados"
    # los ganadores del benchmark deben estar cableados en el flujo
    assert "project-scout" in ctx and "code-architect" in ctx


# ──────────────────────────────────────────────────────────────────────────────
# GRUPO A — Frases alternativas que disparan los 4 flujos del catálogo
# Cobertura: keywords distintos a los del test base, mismos flujos esperados.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
@pytest.mark.parametrize("prompt,expected_fragment", [
    ("construir el modulo de reportes nueva feature", "construir feature"),
    ("cual es mejor decidir entre dos frameworks para el proyecto", "research"),
    ("instalar skill de evaluacion nueva herramienta evaluar", "evaluar"),
    ("trabajar en cliente tocar cliente el crm de", "trabajar"),
])
def test_flow_alternative_phrasings(prompt: str, expected_fragment: str) -> None:
    """Frases alternativas — distintas a las del test base — disparan sus flujos."""
    ctx = _run_hook(prompt)
    assert "🔀 Flujo recomendado" in ctx, f"no se inyectó NINGÚN flujo para: {prompt!r}"
    assert expected_fragment in ctx, (
        f"flujo incorrecto: no contiene '{expected_fragment}' para {prompt!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# GRUPO B — Capacidades opt-in (agents/skills/commands) inyectadas como hints
# Cobertura: verifica que '💡 Capacidades' aparece Y que el texto del hint es correcto.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
@pytest.mark.parametrize("prompt,expected_text", [
    ("backend api microservice endpoint", "software-dev"),
    ("react vue svelte frontend components ui", "frontend"),
    ("trabajar en client-c crm", "client-c"),
    ("deploy to production ahora", "ops:deploy"),
])
def test_capability_hint_is_injected(prompt: str, expected_text: str) -> None:
    """Prompts de dominio específico inyectan '💡 Capacidades' con el hint correcto."""
    ctx = _run_hook(prompt)
    assert "💡 Capacidades" in ctx, (
        f"no se inyectó NINGÚN hint de capacidad para: {prompt!r}"
    )
    assert expected_text in ctx, (
        f"hint presente pero no contiene '{expected_text}' para {prompt!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# GRUPO C — MCP tools de ARIS4U: aris_dialectic y aris_ingest
# Cobertura: los triggers que activan herramientas internas del stack ARIS4U.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
def test_capability_hint_aris_dialectic() -> None:
    """Código sensible → hint de aris_dialectic (review multi-ángulo local)."""
    ctx = _run_hook("review security auth database sensible código sensible")
    assert "💡 Capacidades" in ctx, "no se inyectó hint para código sensible"
    assert "aris_dialectic" in ctx, "falta referencia a aris_dialectic en el hint"


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
def test_capability_hint_aris_ingest() -> None:
    """Decisión importante → hint de aris_ingest para persistir en memoria cross-session."""
    ctx = _run_hook("decision importante guard nuevo")
    assert "💡 Capacidades" in ctx, "no se inyectó hint para 'decision importante'"
    assert any(x in ctx for x in ("aris_ingest", "Ingesta")), (
        "falta referencia a aris_ingest en el hint"
    )


# ──────────────────────────────────────────────────────────────────────────────
# GRUPO D — Precisión: prompts que NO deben disparar flujos de implementación
# Cobertura: documentación y saludos no son falsos positivos de flujo build/research.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
@pytest.mark.parametrize("prompt", [
    "documentacion para el modulo readme",
    "hola",
])
def test_non_implementation_prompts_have_no_flow(prompt: str) -> None:
    """Docs y saludos no provocan falsos positivos en flujos de implementación."""
    ctx = _run_hook(prompt)
    assert "🔀 Flujo recomendado" not in ctx, (
        f"falso positivo: inyectó flujo de implementación para {prompt!r}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# GRUPO E — XFAIL: gaps documentados entre router y hook E2E (no ignorados)
# Estos tests FALLAN intencionalmente hasta que se corrijan los gaps del catálogo.
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
def test_client_b_keyword_triggers_recall_hint() -> None:
    """El keyword 'client-b' activa el flujo trabajar-cliente (fix: triggers JSON 2026-07-04)."""
    ctx = _run_hook("necesito trabajar en client-b plataforma")
    assert "💡 Capacidades" in ctx or "🔀 Flujo" in ctx


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
def test_security_hint_with_implementation_intent() -> None:
    """security-agent acepta intent=implementation (fix: añadido a intent[] en JSON 2026-07-04)."""
    ctx = _run_hook("vulnerability cve pentest exploit")
    assert "💡 Capacidades" in ctx and "security" in ctx.lower()


@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
def test_investiga_espanol_triggers_research_flow() -> None:
    """'investiga' en español dispara flujo-research-decision (fix: triggers JSON 2026-07-04)."""
    ctx = _run_hook("investiga el mejor framework para python")
    assert "🔀 Flujo recomendado" in ctx and "research" in ctx
