"""Tests del cableado del protocolo de orquestación en los hooks (Fase 3).

Cubre la inyección per-turn en UserPromptSubmit (graduada por intención + fail-open +
flag de apagado) y la postura una-vez en el briefing de SessionStart.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"
for _p in (str(HOOKS), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dispatch.events import user_prompt_submit as ups  # noqa: E402
from dispatch.events import _briefing  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_EVENTS_LOG", str(log))
    monkeypatch.delenv("ARIS4U_ORCH_PROTOCOL", raising=False)
    return log


def test_protocol_injected_for_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Inventario FIJO (no el snapshot vivo de disco): los hooks de una sesión Claude
    # activa reescriben data/capability_runtime_snapshot.json durante la suite y una
    # lectura a mitad de write degradaba a set() → flaky (2026-07-01). El camino de
    # disco lo cubren los tests de capability_inventory.
    import tools.orchestration_protocol as op

    monkeypatch.setattr(
        op, "available_capability_names",
        lambda *a, **k: {"aris_search", "aris_dialectic", "second-auditor"},
    )
    parts: list[str] = []
    ups._append_orchestration_protocol(parts, "implementation")
    blob = "\n".join(parts)
    assert "ORQUESTA" in blob
    assert "ENTENDER" in blob and "VERIFICAR" in blob


def test_no_protocol_for_simple() -> None:
    parts: list[str] = []
    ups._append_orchestration_protocol(parts, "simple")
    assert parts == []


def test_protocol_disabled_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_ORCH_PROTOCOL", "0")
    parts: list[str] = []
    ups._append_orchestration_protocol(parts, "implementation")
    assert parts == []


def test_protocol_failopen_when_inventory_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Si build_protocol no encuentra inventario → "" → no se inyecta, no se rompe.
    import tools.orchestration_protocol as op

    monkeypatch.setattr(op, "available_capability_names", lambda *a, **k: set())
    parts: list[str] = []
    ups._append_orchestration_protocol(parts, "implementation")
    assert parts == []


def test_protocol_failopen_on_builder_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tools.orchestration_protocol as op

    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(op, "build_protocol", _boom)
    parts: list[str] = []
    # No debe propagar excepción.
    ups._append_orchestration_protocol(parts, "implementation")
    assert parts == []


def test_briefing_includes_posture() -> None:
    # El briefing real (snapshot del repo) debe incluir la postura si hay inventario.
    posture = _briefing._orchestration_posture_safe()
    if posture:  # solo si el snapshot del repo tiene capacidades
        assert "POSTURA" in posture
        b = _briefing.build_briefing("startup")
        assert "POSTURA" in b
        assert len(b) <= _briefing.BUDGET_CHARS


def test_briefing_posture_failopen(monkeypatch: pytest.MonkeyPatch) -> None:
    import tools.orchestration_protocol as op

    monkeypatch.setattr(op, "build_session_posture", lambda *a, **k: "")
    assert _briefing._orchestration_posture_safe() == ""
    # build_briefing sigue funcionando sin la postura.
    assert _briefing.build_briefing("startup")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
