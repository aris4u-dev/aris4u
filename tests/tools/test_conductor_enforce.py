"""Tests del verify-gate suave (tools/conductor_enforce.py) — OFF por defecto, NUNCA bloquea.

Contrato (Fase 4, reorientado a VERIFICACIÓN):
  - intenciones que producen código: ``implementation`` / ``fix`` (decision/research/simple NO).
  - el recordatorio solo aplica si se TOCÓ código y NO se verificó (ni capacidad de cierre
    adoptada vía hint, ni tests/lint nativos).
  - ``build_stop_reminder`` es PURA (recibe las señales); ``maybe_reminder`` aplica el flag
    y lee las señales runtime de ``tools.verify_gate``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import conductor_enforce as ce  # noqa: E402
from tools import verify_gate as vg  # noqa: E402


# --- build_stop_reminder: política PURA (no flag, no I/O) -------------------- #
def test_reminder_when_code_touched_unverified() -> None:
    # Tocó código (default code_touched=True), diseñó pero NO verificó → recuerda.
    assert "VERIFICAR" in ce.build_stop_reminder("implementation", ["aris-council"])
    assert "VERIFICAR" in ce.build_stop_reminder("fix", [])


def test_no_reminder_when_verified_via_capability() -> None:
    # Adoptó una capacidad de cierre → no recuerda (tolera prefijo de plugin por hoja).
    assert ce.build_stop_reminder("implementation", ["second-auditor"]) == ""
    assert ce.build_stop_reminder("fix", ["aris4u.aris_dialectic"]) == ""


def test_no_reminder_when_native_verified() -> None:
    # Corrió tests/lint nativos (native_verified) → no recuerda aunque no haya capacidad.
    assert ce.build_stop_reminder("implementation", [], native_verified=True) == ""


def test_no_reminder_when_no_code_touched() -> None:
    # No tocó código → nada que verificar.
    assert ce.build_stop_reminder("implementation", [], code_touched=False) == ""
    assert ce.build_stop_reminder("fix", ["aris-council"], code_touched=False) == ""


def test_only_enforced_intents() -> None:
    # Solo implementation/fix; decision/research/simple no producen el nudge de código.
    for intent in ("simple", "research", "decision"):
        assert ce.build_stop_reminder(intent, [], code_touched=True) == ""


# --- maybe_reminder: flag + señales runtime de verify_gate ------------------- #
def test_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIS4U_CONDUCTOR_ENFORCE", raising=False)
    assert ce.is_enforce_on() is False
    # Sin flag NO hay nudge, pase lo que pase → sesión normal intacta.
    assert ce.maybe_reminder("implementation", []) == ""


def test_maybe_reminder_reads_verify_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ARIS4U_CONDUCTOR_ENFORCE", "1")
    monkeypatch.setenv("ARIS4U_VERIFY_STATE", str(tmp_path / "vg.json"))

    # Sin señal de código tocado → no recuerda.
    assert ce.maybe_reminder("implementation", [], "SES") == ""

    # Tocó código y no verificó → recuerda.
    vg.record_tool("SES", "Edit", {"file_path": "/repo/app.py"})
    assert "VERIFICAR" in ce.maybe_reminder("implementation", [], "SES")

    # Tras correr tests nativos → ya no recuerda.
    vg.record_tool("SES", "Bash", {"command": "pytest tests/ -q"})
    assert ce.maybe_reminder("implementation", [], "SES") == ""


def test_maybe_reminder_degrades_gracefully_without_verify_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si verify_gate no está disponible, maybe_reminder no explota y aplica fail-open."""
    monkeypatch.setenv("ARIS4U_CONDUCTOR_ENFORCE", "1")

    # Force ImportError inside maybe_reminder's try/except block.
    import builtins
    real_import = builtins.__import__

    def _broken_import(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "tools" and args and "verify_gate" in str(args):
            raise ImportError("verify_gate unavailable (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _broken_import)
    # With code_touched=True (default in fallback) and no verify, a reminder fires.
    result = ce.maybe_reminder("implementation", [])
    # Must not raise; result is either a reminder string or "".
    assert isinstance(result, str)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
