"""Tests del rastreador de señales de verificación (tools/verify_gate.py).

Cubre: detección de edición de código (genérica multi-stack), detección de verificación
(comandos de tests/lint/types + capacidades de cierre), idempotencia, reset por-turno y
fail-open. Aislado en tmp vía ARIS4U_VERIFY_STATE.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import verify_gate as vg  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_VERIFY_STATE", str(tmp_path / "vg.json"))


@pytest.mark.parametrize(
    "fp", ["/r/a.py", "/r/b.ts", "/r/c.tsx", "/r/d.dart", "/r/e.go", "/r/f.rs", "/r/g.java"]
)
def test_code_edit_detected_multistack(fp: str) -> None:
    vg.record_tool("S", "Edit", {"file_path": fp})
    assert vg.code_was_touched("S") is True
    assert vg.verification_ran("S") is False


def test_non_code_edit_ignored() -> None:
    vg.record_tool("S", "Edit", {"file_path": "/r/README.md"})
    assert vg.code_was_touched("S") is False


def test_write_and_multiedit_count() -> None:
    vg.record_tool("S", "Write", {"file_path": "/r/x.py"})
    assert vg.code_was_touched("S") is True
    vg.record_tool("T", "MultiEdit", {"file_path": "/r/y.kt"})
    assert vg.code_was_touched("T") is True


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest tests/ -q",
        "ruff check .",
        "pyright src/",
        "npm run test",
        "flutter analyze",
        "go test ./...",
        "cargo clippy",
    ],
)
def test_verification_command_detected(cmd: str) -> None:
    vg.record_tool("S", "Bash", {"command": cmd})
    assert vg.verification_ran("S") is True


def test_non_verify_command_ignored() -> None:
    vg.record_tool("S", "Bash", {"command": "git status && ls -la"})
    assert vg.verification_ran("S") is False


@pytest.mark.parametrize(
    "tool,ti",
    [
        ("Skill", {"skill": "second-auditor"}),
        ("Task", {"subagent_type": "code-review"}),
        ("SlashCommand", {"command": "/verify-claims foo"}),
        ("mcp__aris4u__aris_dialectic", {}),
    ],
)
def test_verify_capability_detected(tool: str, ti: dict) -> None:
    vg.record_tool("S", tool, ti)
    assert vg.verification_ran("S") is True


def test_idempotent_signals_only_rise() -> None:
    vg.record_tool("S", "Edit", {"file_path": "/r/a.py"})
    vg.record_tool("S", "Edit", {"file_path": "/r/b.md"})  # no baja code_touched
    assert vg.code_was_touched("S") is True


def test_reset_session_clears() -> None:
    vg.record_tool("S", "Edit", {"file_path": "/r/a.py"})
    vg.record_tool("S", "Bash", {"command": "pytest"})
    vg.reset_session("S")
    assert vg.code_was_touched("S") is False
    assert vg.verification_ran("S") is False


def test_failopen_unknown_session() -> None:
    assert vg.code_was_touched("nope") is False
    assert vg.verification_ran("nope") is False
    vg.reset_session("nope")  # no debe lanzar


def test_failopen_bad_input() -> None:
    # tool_input no-dict no debe romper.
    vg.record_tool("S", "Edit", None)  # type: ignore[arg-type]
    assert vg.code_was_touched("S") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
