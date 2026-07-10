"""Tests del guard bloqueante de gobierno de modelos.

Verifica el comportamiento de ~/.claude/hooks/model-routing-guard.py
llamándolo como subproceso (igual que Claude Code en producción).

El guard lee JSON de stdin, escribe en stderr si bloquea, y sale con:
  - exit 0 → PASS (deja pasar la tool call)
  - exit 2 → BLOCK (bloquea la tool call)

Casos:
  1.  Agent con model= explícito → PASS
  2.  Agent sin model= ni subagent_type → BLOCK
  3.  Agent con subagent_type cuyo frontmatter fija modelo → PASS
  4.  Agent con subagent_type inexistente (sin frontmatter) → BLOCK
  5.  Task con model= → PASS
  6.  Task sin model= → BLOCK
  7.  Workflow con script que tiene agent() y model: → PASS
  8.  Workflow con script que tiene agent() y CERO model: → BLOCK
  9.  Workflow por nombre (sin script) → PASS (ya auditado en disco)
  10. Herramienta no relevante / payload mínimo → PASS (fail-open)
  11. pass^k: el caso más crítico (BLOCK) debe ser determinista (k=5)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration]

GUARD = Path.home() / ".claude" / "hooks" / "model-routing-guard.py"
PYTHON = sys.executable

# Agente real con model: sonnet en frontmatter (verificado en ~/.claude/agents/)
KNOWN_AGENT_WITH_MODEL = "software-dev"


def run_guard(payload: dict) -> "subprocess.CompletedProcess[str]":
    """Ejecuta el guard con el payload como stdin JSON."""
    return subprocess.run(
        [PYTHON, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Precondición: el guard existe y es ejecutable
# ---------------------------------------------------------------------------

def test_guard_exists() -> None:
    """El archivo del guard debe existir."""
    assert GUARD.is_file(), f"Guard no encontrado: {GUARD}"


def test_known_agent_has_frontmatter_model() -> None:
    """El agente de referencia debe tener model: en su frontmatter."""
    agent_file = Path.home() / ".claude" / "agents" / f"{KNOWN_AGENT_WITH_MODEL}.md"
    assert agent_file.is_file(), f"Agente no encontrado: {agent_file}"
    content = agent_file.read_text(encoding="utf-8")
    assert "model:" in content, (
        f"El agente {KNOWN_AGENT_WITH_MODEL} no tiene 'model:' en su frontmatter"
    )


# ---------------------------------------------------------------------------
# Caso 1: Agent con model= explícito → PASS (exit 0)
# ---------------------------------------------------------------------------

def test_agent_with_explicit_model_passes() -> None:
    """Agent con model= explícito debe pasar sin bloquear."""
    payload = {"tool_name": "Agent", "tool_input": {"model": "sonnet", "prompt": "hola"}}
    result = run_guard(payload)
    assert result.returncode == 0, (
        f"Esperado exit 0, obtenido {result.returncode}. stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Caso 2: Agent sin model= y sin subagent_type → BLOCK (exit 2)
# ---------------------------------------------------------------------------

def test_agent_no_model_no_subagent_type_blocks() -> None:
    """Agent sin model= ni subagent_type debe ser bloqueado."""
    payload = {"tool_name": "Agent", "tool_input": {"prompt": "hola"}}
    result = run_guard(payload)
    assert result.returncode == 2, (
        f"Esperado exit 2 (BLOCK), obtenido {result.returncode}. stderr={result.stderr!r}"
    )
    assert "GOBIERNO DE MODELOS" in result.stderr, (
        "El mensaje de bloqueo debe mencionar 'GOBIERNO DE MODELOS'"
    )


# ---------------------------------------------------------------------------
# Caso 3: Agent con subagent_type cuyo frontmatter fija modelo → PASS
# ---------------------------------------------------------------------------

def test_agent_subagent_type_with_frontmatter_model_passes() -> None:
    """Agent con subagent_type que tiene model: en frontmatter debe pasar."""
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": KNOWN_AGENT_WITH_MODEL},
    }
    result = run_guard(payload)
    assert result.returncode == 0, (
        f"Esperado exit 0 para subagent_type='{KNOWN_AGENT_WITH_MODEL}', "
        f"obtenido {result.returncode}. stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Caso 4: Agent con subagent_type inexistente (sin frontmatter) → BLOCK
# ---------------------------------------------------------------------------

def test_agent_nonexistent_subagent_type_blocks() -> None:
    """Agent con subagent_type sin frontmatter conocido debe ser bloqueado."""
    payload = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "nonexistent-agent-xyz-404"},
    }
    result = run_guard(payload)
    assert result.returncode == 2, (
        f"Esperado exit 2 (BLOCK), obtenido {result.returncode}. stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Caso 5: Task con model= → PASS (exit 0)
# ---------------------------------------------------------------------------

def test_task_with_model_passes() -> None:
    """Task con model= debe pasar sin bloquear."""
    payload = {"tool_name": "Task", "tool_input": {"model": "opus", "description": "test"}}
    result = run_guard(payload)
    assert result.returncode == 0, (
        f"Esperado exit 0, obtenido {result.returncode}. stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Caso 6: Task sin model= → BLOCK (exit 2)
# ---------------------------------------------------------------------------

def test_task_no_model_blocks() -> None:
    """Task sin model= debe ser bloqueado."""
    payload = {"tool_name": "Task", "tool_input": {"description": "test"}}
    result = run_guard(payload)
    assert result.returncode == 2, (
        f"Esperado exit 2 (BLOCK), obtenido {result.returncode}. stderr={result.stderr!r}"
    )
    assert "GOBIERNO DE MODELOS" in result.stderr


# ---------------------------------------------------------------------------
# Caso 7: Workflow con script que tiene agent() y model: → PASS
# ---------------------------------------------------------------------------

def test_workflow_script_with_agent_and_model_passes() -> None:
    """Workflow con script que incluye agent() + model: debe pasar."""
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"script": "const r = await agent('hola', {model: 'sonnet'})"},
    }
    result = run_guard(payload)
    assert result.returncode == 0, (
        f"Esperado exit 0, obtenido {result.returncode}. stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Caso 8: Workflow con script que tiene agent() y CERO model: → BLOCK
# ---------------------------------------------------------------------------

def test_workflow_script_with_agent_no_model_blocks() -> None:
    """Workflow con script que tiene agent() pero cero model: debe ser bloqueado."""
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"script": "const r = await agent('hola')"},
    }
    result = run_guard(payload)
    assert result.returncode == 2, (
        f"Esperado exit 2 (BLOCK), obtenido {result.returncode}. stderr={result.stderr!r}"
    )
    assert "GOBIERNO DE MODELOS" in result.stderr
    assert "model:" in result.stderr or "model" in result.stderr


# ---------------------------------------------------------------------------
# Caso 9: Workflow por nombre (sin script) → PASS (ya auditado en disco)
# ---------------------------------------------------------------------------

def test_workflow_by_name_no_script_passes() -> None:
    """Workflow lanzado por nombre (sin script inline) debe pasar (ya auditado)."""
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": "mi-workflow"},
    }
    result = run_guard(payload)
    assert result.returncode == 0, (
        f"Esperado exit 0 para Workflow por nombre, "
        f"obtenido {result.returncode}. stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Caso 10: Herramienta no relevante → PASS (fail-open)
# ---------------------------------------------------------------------------

def test_irrelevant_tool_passes() -> None:
    """Herramienta no relevante (Bash) debe pasar sin bloquear (fail-open)."""
    payload = {"tool_name": "Bash", "tool_input": {"command": "echo hola"}}
    result = run_guard(payload)
    assert result.returncode == 0, (
        f"Esperado exit 0 para tool Bash, obtenido {result.returncode}. stderr={result.stderr!r}"
    )


def test_empty_payload_passes() -> None:
    """Payload vacío {} debe pasar sin bloquear (fail-open)."""
    result = run_guard({})
    assert result.returncode == 0, (
        f"Esperado exit 0 para payload vacío, obtenido {result.returncode}"
    )


def test_malformed_stdin_passes() -> None:
    """Stdin con JSON inválido debe pasar sin bloquear (fail-open)."""
    result = subprocess.run(
        [PYTHON, str(GUARD)],
        input="esto no es json {{{",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Esperado exit 0 ante JSON inválido, obtenido {result.returncode}"
    )


# ---------------------------------------------------------------------------
# Caso 11: pass^k — determinismo del bloqueo más crítico (k=5)
# ---------------------------------------------------------------------------

def test_guard_blocks_agent_no_model_pass_k() -> None:
    """pass^k: el guard debe bloquear SIEMPRE (k=5) sin flakiness."""
    payload = {"tool_name": "Agent", "tool_input": {"prompt": "hola"}}
    results = [run_guard(payload).returncode for _ in range(5)]
    assert all(r == 2 for r in results), (
        f"El guard no fue determinista en k=5 ejecuciones: {results}"
    )
