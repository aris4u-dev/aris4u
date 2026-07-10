"""Tests de caracterización del handler SubagentStart (depth propagation).

Caracteriza el comportamiento ACTUAL de
`hooks/dispatch/events/subagent_start.py::handle` (CC=37, sin tests previos),
como red de seguridad ANTES de cualquier refactor. Los asserts describen lo que
el código hace HOY; no son aspiracionales.

Patrón: IN-PROCESS con mocks (más preciso que el subproceso para caracterizar
ramas individuales). Estrategia:

  - `emit_additional_context` (importado en el módulo) hace `sys.exit`. Se
    monkeypatchea por un capturador que guarda el texto emitido en una lista,
    así `handle()` retorna normalmente y podemos inspeccionar la salida.
  - Los imports de `engine.v16.*` son LAZY dentro de `handle` (`from
    engine.v16.session_manager import ...`), por lo que se monkeypatchean en el
    módulo `engine.v16.session_manager` / `engine.v16.agent_orchestrator` ANTES
    de invocar `handle` (el bind ocurre en runtime).
  - `STATE_FILE` y `SESSIONS_DB` se redirigen a `tmp_path` vía monkeypatch de
    `ss.STATE_FILE` / `ss.SESSIONS_DB` para NO tocar /tmp ni la DB real y para
    aislar los tests entre sí (Regla #2: nunca tocar estado global real).

Ramas cubiertas:
  - Header de depth propagation + bloque QUALITY REQUIREMENTS (4 bullets).
  - LOCKED DECISIONS: presente con last_query + decisiones; ausente sin query
    y sin DB.
  - CRITICAL GUARDS: dedup + cap a 6; ausente sin guards critical.
  - FAIL-OPEN: excepción en get_all_guards; JSON inválido en STATE_FILE.
  - state tracking: research_agents_launched incrementa, tools_used contiene Agent.

Corre:
    .venv312/bin/python3 -m pytest tests/dispatch/test_subagent_start.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"

# hooks/ y ROOT en sys.path para importar el módulo del handler y engine.v16.*
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dispatch.events import subagent_start as ss  # noqa: E402
from engine.v16 import agent_orchestrator as orch_mod  # noqa: E402
from engine.v16 import session_manager as sm  # noqa: E402


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Captura el texto que el handler pasa a emit_additional_context.

    Reemplaza ss.emit_additional_context (que normalmente hace sys.exit) por un
    capturador que NO sale, permitiendo que handle() retorne y el test inspeccione.

    Returns:
        Lista a la que se anexa cada contexto emitido (típicamente 1 elemento).
    """
    sink: list[str] = []

    def _capture(context: str) -> None:
        sink.append(context)

    monkeypatch.setattr(ss, "emit_additional_context", _capture)
    return sink


@pytest.fixture
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirige ss.STATE_FILE a un archivo en tmp_path (no toca /tmp real).

    Returns:
        El Path del state file aislado (no existe aún salvo que el test lo cree).
    """
    state_file = tmp_path / "session_state.json"
    monkeypatch.setattr(ss, "STATE_FILE", state_file)
    return state_file


@pytest.fixture
def db_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Hace que ss.SESSIONS_DB exista (apuntando a un archivo creado en tmp_path).

    Necesario para activar las ramas LOCKED DECISIONS y CRITICAL GUARDS, que
    están gateadas por `SESSIONS_DB.exists()`.

    Returns:
        El Path del archivo DB de mentira (existe en disco, contenido irrelevante).
    """
    db = tmp_path / "sessions.db"
    db.write_text("")  # solo necesita existir; el handler no lo abre directamente
    monkeypatch.setattr(ss, "SESSIONS_DB", db)
    return db


@pytest.fixture
def db_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Hace que ss.SESSIONS_DB NO exista → ramas LOCKED/GUARDS desactivadas.

    Returns:
        El Path inexistente al que apunta SESSIONS_DB.
    """
    db = tmp_path / "nonexistent.db"
    monkeypatch.setattr(ss, "SESSIONS_DB", db)
    return db


def _emitted(sink: list[str]) -> str:
    """Devuelve el único texto emitido por handle(), fallando si no hubo exactamente uno.

    Args:
        sink: La lista poblada por el fixture `captured`.

    Returns:
        El string de additionalContext emitido.
    """
    assert len(sink) == 1, f"se esperaba exactamente 1 emisión, hubo {len(sink)}"
    return sink[0]


# ---------------------------------------------------------------------------
# Header + QUALITY REQUIREMENTS — siempre presentes
# ---------------------------------------------------------------------------

def test_always_emits_header(
    captured: list[str], isolated_state: Path, db_absent: Path
) -> None:
    """handle() siempre emite el header de depth propagation."""
    ss.handle("SubagentStart", {})
    out = _emitted(captured)
    assert "[ARIS4U DEPTH PROPAGATION" in out


def test_always_emits_quality_requirements_with_four_bullets(
    captured: list[str], isolated_state: Path, db_absent: Path
) -> None:
    """La sección QUALITY REQUIREMENTS y sus 4 bullets siempre se emiten."""
    ss.handle("SubagentStart", {})
    out = _emitted(captured)
    assert "QUALITY REQUIREMENTS:" in out
    assert "- Write COMPLETE code, not skeletons or TODOs" in out
    assert "- Include input validation and error handling" in out
    assert "- Verify your work compiles/runs before returning" in out
    assert "- If implementation: describe user-testable verification steps" in out


# ---------------------------------------------------------------------------
# LOCKED DECISIONS
# ---------------------------------------------------------------------------

def test_locked_decisions_present_with_query_and_results(
    captured: list[str],
    isolated_state: Path,
    db_present: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con last_query y get_locked_decisions devolviendo resultados, aparecen las decisiones."""
    isolated_state.write_text(json.dumps({"last_query": "how to handle migrations"}))

    def _fake_locked(query: str, limit: int = 5) -> list[dict]:
        return [
            {"session_ref": "0601a", "decision": "Use Flyway for all migrations"},
            {"session_ref": "0602b", "decision": "RLS mandatory on PHI tables"},
        ]

    monkeypatch.setattr(sm, "get_locked_decisions", _fake_locked)
    # Sin guards critical para aislar esta rama.
    monkeypatch.setattr(sm, "get_all_guards", lambda: [])

    ss.handle("SubagentStart", {})
    out = _emitted(captured)
    assert "LOCKED DECISIONS (do NOT contradict):" in out
    assert "- [0601a] Use Flyway for all migrations" in out
    assert "- [0602b] RLS mandatory on PHI tables" in out


def test_locked_decisions_absent_without_query(
    captured: list[str],
    isolated_state: Path,
    db_present: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin last_query en el estado, el bloque LOCKED DECISIONS NO aparece."""
    isolated_state.write_text(json.dumps({}))  # sin last_query

    def _fake_locked(query: str, limit: int = 5) -> list[dict]:
        return [{"session_ref": "x", "decision": "should not be reached"}]

    monkeypatch.setattr(sm, "get_locked_decisions", _fake_locked)
    monkeypatch.setattr(sm, "get_all_guards", lambda: [])

    ss.handle("SubagentStart", {})
    out = _emitted(captured)
    assert "LOCKED DECISIONS" not in out


def test_locked_decisions_absent_with_query_but_empty_results(
    captured: list[str],
    isolated_state: Path,
    db_present: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con last_query pero get_locked_decisions devolviendo [], el bloque NO aparece."""
    isolated_state.write_text(json.dumps({"last_query": "anything"}))
    monkeypatch.setattr(sm, "get_locked_decisions", lambda query, limit=5: [])
    monkeypatch.setattr(sm, "get_all_guards", lambda: [])

    ss.handle("SubagentStart", {})
    out = _emitted(captured)
    assert "LOCKED DECISIONS" not in out


def test_locked_decisions_absent_when_db_missing(
    captured: list[str],
    isolated_state: Path,
    db_absent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aunque haya last_query, sin SESSIONS_DB el bloque LOCKED DECISIONS NO aparece."""
    isolated_state.write_text(json.dumps({"last_query": "anything"}))

    def _boom(query: str, limit: int = 5) -> list[dict]:
        raise AssertionError("get_locked_decisions no debe llamarse sin DB")

    monkeypatch.setattr(sm, "get_locked_decisions", _boom)
    monkeypatch.setattr(sm, "get_all_guards", lambda: [])

    ss.handle("SubagentStart", {})
    out = _emitted(captured)
    assert "LOCKED DECISIONS" not in out


# ---------------------------------------------------------------------------
# CRITICAL GUARDS — dedup + cap a 6
# ---------------------------------------------------------------------------

def test_critical_guards_dedup_and_cap_at_six(
    captured: list[str],
    isolated_state: Path,
    db_present: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards critical duplicados se deduplican y la lista se limita a 6 patrones."""
    # 8 patrones únicos + 2 duplicados → tras dedup quedan 8, capados a 6.
    guards = [
        {"severity": "critical", "pattern": f"pattern-{i}"} for i in range(8)
    ]
    guards.append({"severity": "critical", "pattern": "pattern-0"})  # dup
    guards.append({"severity": "critical", "pattern": "pattern-1"})  # dup
    # Un guard no-critical que debe ignorarse.
    guards.append({"severity": "warning", "pattern": "ignored-warning"})

    monkeypatch.setattr(sm, "get_all_guards", lambda: guards)
    monkeypatch.setattr(sm, "get_locked_decisions", lambda query, limit=5: [])

    ss.handle("SubagentStart", {})
    out = _emitted(captured)

    assert "CRITICAL GUARDS:" in out
    # Exactamente 6 bullets de patrón (los primeros 6 únicos).
    guard_lines = [
        ln for ln in out.splitlines() if ln.startswith("- pattern-")
    ]
    assert len(guard_lines) == 6, f"esperaba 6 guards, hubo {len(guard_lines)}: {guard_lines}"
    # Dedup: ningún patrón repetido.
    assert len(set(guard_lines)) == 6
    # El warning no-critical no aparece.
    assert "ignored-warning" not in out


def test_critical_guards_absent_without_critical(
    captured: list[str],
    isolated_state: Path,
    db_present: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin guards de severity 'critical', el bloque CRITICAL GUARDS NO aparece."""
    guards = [
        {"severity": "warning", "pattern": "w1"},
        {"severity": "info", "pattern": "i1"},
    ]
    monkeypatch.setattr(sm, "get_all_guards", lambda: guards)
    monkeypatch.setattr(sm, "get_locked_decisions", lambda query, limit=5: [])

    ss.handle("SubagentStart", {})
    out = _emitted(captured)
    assert "CRITICAL GUARDS" not in out


# ---------------------------------------------------------------------------
# FAIL-OPEN
# ---------------------------------------------------------------------------

def test_failopen_get_all_guards_raises(
    captured: list[str],
    isolated_state: Path,
    db_present: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si get_all_guards lanza, handle() no crashea y aun emite QUALITY REQUIREMENTS."""
    def _boom() -> list[dict]:
        raise RuntimeError("db blew up")

    monkeypatch.setattr(sm, "get_all_guards", _boom)
    monkeypatch.setattr(sm, "get_locked_decisions", lambda query, limit=5: [])

    ss.handle("SubagentStart", {})  # no debe lanzar
    out = _emitted(captured)
    assert "QUALITY REQUIREMENTS:" in out
    # La excepción se traga → no se emite el bloque de guards.
    assert "CRITICAL GUARDS" not in out


def test_failopen_invalid_state_json(
    captured: list[str],
    isolated_state: Path,
    db_absent: Path,
) -> None:
    """STATE_FILE con JSON inválido no rompe handle(); igual emite el contexto."""
    isolated_state.write_text("{ this is not valid json :::")
    ss.handle("SubagentStart", {})  # no debe lanzar
    out = _emitted(captured)
    assert "[ARIS4U DEPTH PROPAGATION" in out
    assert "QUALITY REQUIREMENTS:" in out


def test_failopen_session_manager_import_unavailable(
    captured: list[str],
    isolated_state: Path,
    db_present: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si las funciones del session_manager fallan al resolver, handle() sigue (fail-open).

    Se simula el caso 'engine no disponible' poniendo ambas funciones a una que
    lanza; las ramas LOCKED/GUARDS no producen output pero QUALITY REQUIREMENTS sí.
    """
    monkeypatch.setattr(sm, "get_all_guards", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(
        sm, "get_locked_decisions", lambda query, limit=5: (_ for _ in ()).throw(RuntimeError("y"))
    )
    isolated_state.write_text(json.dumps({"last_query": "q"}))

    ss.handle("SubagentStart", {})
    out = _emitted(captured)
    assert "QUALITY REQUIREMENTS:" in out
    assert "LOCKED DECISIONS" not in out
    assert "CRITICAL GUARDS" not in out


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def test_state_tracking_increments_and_records_agent(
    captured: list[str],
    isolated_state: Path,
    db_absent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tras handle(), research_agents_launched incrementa y tools_used contiene 'Agent'."""
    # Pre-poblar con un conteo previo para verificar el incremento.
    isolated_state.write_text(json.dumps({"research_agents_launched": 2, "tools_used": []}))

    ss.handle("SubagentStart", {})

    written = json.loads(isolated_state.read_text())
    assert written["research_agents_launched"] == 3
    assert "Agent" in written["tools_used"]


def test_state_tracking_from_empty_state(
    captured: list[str],
    isolated_state: Path,
    db_absent: Path,
) -> None:
    """Sin state previo, handle() inicializa el conteo en 1 y registra 'Agent' una vez."""
    # STATE_FILE no existe al inicio.
    assert not isolated_state.exists()
    ss.handle("SubagentStart", {})

    written = json.loads(isolated_state.read_text())
    assert written["research_agents_launched"] == 1
    assert written["tools_used"] == ["Agent"]


def test_state_tracking_does_not_duplicate_agent(
    captured: list[str],
    isolated_state: Path,
    db_absent: Path,
) -> None:
    """Si 'Agent' ya está en tools_used, no se duplica tras handle()."""
    isolated_state.write_text(
        json.dumps({"research_agents_launched": 1, "tools_used": ["Agent"]})
    )
    ss.handle("SubagentStart", {})

    written = json.loads(isolated_state.read_text())
    assert written["tools_used"].count("Agent") == 1
    assert written["research_agents_launched"] == 2


# ---------------------------------------------------------------------------
# Orchestrator wave plan
# ---------------------------------------------------------------------------

def test_agent_execution_plan_emitted_when_waves_exist(
    captured: list[str],
    isolated_state: Path,
    db_absent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con waves del orquestador, aparece AGENT EXECUTION PLAN y el progreso."""

    class _FakeOrch:
        def __init__(self) -> None:
            pass

        def get_waves(self) -> list[list[str]]:
            return [["research-agent", "audit-agent"], ["impl-agent"]]

        def summary(self) -> dict:
            return {"completed": 1, "total": 3}

    monkeypatch.setattr(orch_mod, "AgentOrchestrator", _FakeOrch)

    ss.handle("SubagentStart", {})
    out = _emitted(captured)
    assert "AGENT EXECUTION PLAN:" in out
    assert "Wave 1: research-agent, audit-agent" in out
    assert "Wave 2: impl-agent" in out
    assert "Progress: 1/3 completed" in out


def test_agent_execution_plan_absent_when_no_waves(
    captured: list[str],
    isolated_state: Path,
    db_absent: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin waves, el bloque AGENT EXECUTION PLAN NO aparece."""

    class _FakeOrch:
        def __init__(self) -> None:
            pass

        def get_waves(self) -> list[list[str]]:
            return []

        def summary(self) -> dict:
            return {"completed": 0, "total": 0}

    monkeypatch.setattr(orch_mod, "AgentOrchestrator", _FakeOrch)

    ss.handle("SubagentStart", {})
    out = _emitted(captured)
    assert "AGENT EXECUTION PLAN" not in out
