"""Tests de integración del lazo Fase 4 en los hooks Stop y PostToolUse.

Verifica que: (1) PostToolUse marca adopción cuando el modelo usa un hint, (2) Stop cierra
los ignorados SIEMPRE, (3) el nudge de enforcement está OFF por defecto (sesión normal
intacta) y solo emite un block con el flag ON. Todo aislado en tmp.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"
for p in (str(ROOT), str(HOOKS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dispatch.events import post_tool_use as ptu  # noqa: E402
from dispatch.events import stop  # noqa: E402
from tools import capability_adoption as ca  # noqa: E402
from tools import verify_gate as vg  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ARIS4U_HINT_STATE", str(tmp_path / "pending.json"))
    monkeypatch.setenv("ARIS4U_VERIFY_STATE", str(tmp_path / "verify.json"))
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_EVENTS_LOG", str(log))
    monkeypatch.setenv("ARIS4U_SESSION_ID", "SES")
    monkeypatch.delenv("ARIS4U_CONDUCTOR_ENFORCE", raising=False)
    # Aísla el verificador de Stop del log de PRODUCCIÓN: un log_file inexistente hace que
    # handle() haga passthrough rápido tras el cierre del conductor (sin correr el verifier).
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(tmp_path / "vlog.jsonl"))
    return log


def _events(log: Path) -> list[dict]:
    return [json.loads(x) for x in log.read_text().splitlines() if x.strip()] if log.exists() else []


# --- PostToolUse: hint → uso → adopted -------------------------------------- #
def test_post_tool_use_records_adoption(_isolate: Path) -> None:
    ca.register_hints("SES", ["aris4u.aris_recall_client"], intent="implementation")
    ptu._record_adoption("mcp__aris4u__aris_recall_client", {})
    assert any(e["event"] == "capability_adopted" for e in _events(_isolate))


def test_post_tool_use_handle_does_not_crash(_isolate: Path) -> None:
    ca.register_hints("SES", ["aris-council"], intent="decision")
    with pytest.raises(SystemExit) as ex:
        ptu.handle("PostToolUse", {"tool_name": "Skill", "tool_input": {"skill": "aris-council"}})
    assert ex.value.code == 0
    assert any(e["event"] == "capability_adopted" for e in _events(_isolate))


# --- Stop: ventana de 2 turnos ------------------------------------------------ #
def test_conductor_close_flushes_ignored_after_two_turns(_isolate: Path) -> None:
    """Un hint sin adoptar necesita 2 flushes para quedar como ignored (ventana 2 turnos)."""
    ca.register_hints("SES", ["status"], intent="decision")  # nunca adoptado

    # Turno 1: primer flush → hint sobrevive con turn_age=1, SIN capability_ignored
    reminder = stop._conductor_close("SES")
    assert reminder == ""  # flag OFF
    assert not any(e["event"] == "capability_ignored" for e in _events(_isolate))

    # Turno 2: segundo flush → AHORA se marca como capability_ignored
    stop._conductor_close("SES")
    assert any(e["event"] == "capability_ignored" and e["name"] == "status" for e in _events(_isolate))


# --- Enforcement OFF por defecto: sesión normal intacta --------------------- #
def test_stop_handle_no_block_when_flag_off(_isolate: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ca.register_hints("SES", ["aris-council"], intent="implementation")  # sin verificar
    with pytest.raises(SystemExit) as ex:
        stop.handle("Stop", {})  # sin log_file → passthrough tras el cierre del conductor
    assert ex.value.code == 0
    assert capsys.readouterr().out.strip() == ""  # NO emitió block


# --- Enforcement ON: recordatorio SUAVE (additionalContext), NUNCA bloqueo ---- #
def test_stop_handle_soft_reminder_when_flag_on(_isolate: Path, monkeypatch: pytest.MonkeyPatch,
                                                capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ARIS4U_CONDUCTOR_ENFORCE", "1")
    ca.register_hints("SES", ["aris-council"], intent="implementation")
    vg.record_tool("SES", "Edit", {"file_path": "/repo/app.py"})  # tocó código, no verificó
    with pytest.raises(SystemExit) as ex:
        stop.handle("Stop", {})
    assert ex.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    # SUAVE: additionalContext (no ``decision:block``, que forzaría a continuar).
    assert "decision" not in payload
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "VERIFICAR" in payload["hookSpecificOutput"]["additionalContext"]
    assert "VERIFICAR" in payload["systemMessage"]


def test_stop_handle_no_reminder_when_no_code(_isolate: Path, monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    # Flag ON pero NO se tocó código → no hay nada que verificar → sin recordatorio.
    monkeypatch.setenv("ARIS4U_CONDUCTOR_ENFORCE", "1")
    ca.register_hints("SES", ["aris-council"], intent="implementation")
    with pytest.raises(SystemExit) as ex:
        stop.handle("Stop", {})
    assert ex.value.code == 0
    assert capsys.readouterr().out.strip() == ""


def test_stop_handle_no_reminder_when_verified(_isolate: Path, monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("ARIS4U_CONDUCTOR_ENFORCE", "1")
    ca.register_hints("SES", ["second-auditor"], intent="implementation")
    vg.record_tool("SES", "Edit", {"file_path": "/repo/app.py"})  # tocó código
    ca.record_tool_use("SES", "Skill", {"skill": "second-auditor"})  # …pero SÍ verificó
    with pytest.raises(SystemExit) as ex:
        stop.handle("Stop", {})
    assert ex.value.code == 0
    assert capsys.readouterr().out.strip() == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
