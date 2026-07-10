"""Tests de la telemetría de adopción (Fase 4): hint → uso → adopted/ignored.

Cubre: el matcher GENÉRICO invocación→capacidad, el ciclo register→record→flush, la
emisión de capability_adopted / capability_ignored, y el fail-open (config ausente).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import capability_adoption as ca  # type: ignore[import-not-found]  # noqa: E402


@pytest.fixture(autouse=True)
def adoption_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Aísla estado pendiente y event log en tmp (cero contaminación de producción)."""
    monkeypatch.setenv("ARIS4U_HINT_STATE", str(tmp_path / "pending.json"))
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_EVENTS_LOG", str(log))
    return log


def _events(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(x) for x in log.read_text().splitlines() if x.strip()]


# --------------------------------------------------------------------------- #
# Matcher GENÉRICO (sin nombres de cliente)
# --------------------------------------------------------------------------- #
def test_identifiers_mcp() -> None:
    ids = ca.invocation_identifiers("mcp__aris4u__aris_recall_client", {})
    assert "aris4u.aris_recall_client" in ids
    assert "aris_recall_client" in ids


def test_identifiers_task_agent() -> None:
    ids = ca.invocation_identifiers("Task", {"subagent_type": "code-review-agent"})
    assert "code-review-agent" in ids


def test_identifiers_skill_qualified() -> None:
    ids = ca.invocation_identifiers("Skill", {"skill": "plugin-dev:hook-development"})
    assert "plugin-dev:hook-development" in ids
    assert "hook-development" in ids  # hoja


def test_identifiers_slashcommand() -> None:
    ids = ca.invocation_identifiers("SlashCommand", {"command": "/aris-council ¿qué se me escapa?"})
    assert "aris-council" in ids


def test_matches_full_and_leaf() -> None:
    ids = ca.invocation_identifiers("Skill", {"skill": "aris-council"})
    assert ca._matches("aris-council", ids)
    assert not ca._matches("status", ids)


# --------------------------------------------------------------------------- #
# Ciclo hint → uso: adopted
# --------------------------------------------------------------------------- #
def test_cycle_hint_then_tool_use_marks_adopted(adoption_env: Path) -> None:
    sid = "S1"
    ca.register_hints(sid, ["aris-council", "aris4u.aris_recall_client"], intent="decision")
    # El modelo invoca el recall MCP → adopta esa capacidad.
    adopted = ca.record_tool_use(sid, "mcp__aris4u__aris_recall_client", {})
    assert adopted == ["aris4u.aris_recall_client"]
    evs = [e for e in _events(adoption_env) if e["event"] == "capability_adopted"]
    assert len(evs) == 1
    assert evs[0]["name"] == "aris4u.aris_recall_client"
    assert evs[0]["intent"] == "decision"


def test_adopted_not_double_counted(adoption_env: Path) -> None:
    sid = "S2"
    ca.register_hints(sid, ["aris-council"], intent="decision")
    ca.record_tool_use(sid, "Skill", {"skill": "aris-council"})
    ca.record_tool_use(sid, "Skill", {"skill": "aris-council"})  # repetido
    evs = [e for e in _events(adoption_env) if e["event"] == "capability_adopted"]
    assert len(evs) == 1


def test_unrelated_tool_use_no_adoption(adoption_env: Path) -> None:
    sid = "S3"
    ca.register_hints(sid, ["aris-council"], intent="decision")
    assert ca.record_tool_use(sid, "Bash", {"command": "ls"}) == []
    assert [e for e in _events(adoption_env) if e["event"] == "capability_adopted"] == []


# --------------------------------------------------------------------------- #
# Cierre del turno: ignored
# --------------------------------------------------------------------------- #
def test_flush_marks_unadopted_as_ignored(adoption_env: Path) -> None:
    """Ventana 2 turnos: el hint ignorado requiere 2 flushes para quedar como capability_ignored."""
    sid = "S4"
    ca.register_hints(sid, ["aris-council", "status"], intent="decision")
    ca.record_tool_use(sid, "Skill", {"skill": "aris-council"})  # solo una adoptada
    ca.flush_ignored(sid)            # turno 1: status→turn_age=1, sobrevive
    ignored = ca.flush_ignored(sid)  # turno 2: status→capability_ignored
    assert ignored == ["status"]
    evs = _events(adoption_env)
    assert any(e["event"] == "capability_ignored" and e["name"] == "status" for e in evs)
    # adoptada no se reporta como ignorada
    assert not any(e["event"] == "capability_ignored" and e["name"] == "aris-council" for e in evs)


def test_flush_is_idempotent() -> None:
    """Después de la ventana de 2 turnos, un tercer flush no emite nada (ya cerrado)."""
    sid = "S5"
    ca.register_hints(sid, ["status"], intent="decision")
    ca.flush_ignored(sid)              # turno 1: status→turn_age=1
    ca.flush_ignored(sid)              # turno 2: status→capability_ignored
    third = ca.flush_ignored(sid)      # turno 3: ya cerrado, sin nada pendiente
    assert third == []


def test_register_preserves_surviving_hints_across_turns(adoption_env: Path) -> None:
    """register_hints preserva hints con turn_age=1; el cierre ocurre en el 2do flush (Stop)."""
    sid = "S6"
    ca.register_hints(sid, ["status"], intent="decision")  # turno 1: turn_age=0
    ca.flush_ignored(sid)  # stop turno 1: status→turn_age=1, SIN capability_ignored todavía
    evs_after_first_flush = _events(adoption_env)
    assert not any(e["event"] == "capability_ignored" for e in evs_after_first_flush)

    ca.register_hints(sid, ["aris-council"], intent="decision")  # turno 2: status preservado
    ca.flush_ignored(sid)  # stop turno 2: status→capability_ignored ahora
    evs = _events(adoption_env)
    assert any(e["event"] == "capability_ignored" and e["name"] == "status" for e in evs)


def test_peek_session() -> None:
    sid = "S7"
    ca.register_hints(sid, ["aris-council", "status"], intent="implementation")
    ca.record_tool_use(sid, "Skill", {"skill": "aris-council"})
    intent, adopted = ca.peek_session(sid)
    assert intent == "implementation"
    assert adopted == ["aris-council"]


# --------------------------------------------------------------------------- #
# FAIL-OPEN: config/estado ausente o corrupto → neutral, nunca lanza
# --------------------------------------------------------------------------- #
def test_record_without_pending_is_noop() -> None:
    assert ca.record_tool_use("ZZZ", "mcp__aris4u__aris_recall_client", {}) == []


def test_corrupt_state_failopen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "pending.json"
    p.write_text("{not json")
    monkeypatch.setenv("ARIS4U_HINT_STATE", str(p))
    # No debe lanzar; degrada a estado vacío.
    ca.register_hints("S", ["aris-council"], intent="decision")
    assert ca.peek_session("S")[0] == "decision"


def test_empty_hints_noop() -> None:
    ca.register_hints("S8", [], intent="decision")
    assert ca.peek_session("S8") == ("", [])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
