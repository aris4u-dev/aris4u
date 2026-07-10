"""Integración subprocess: valida que los handlers PreToolUse/PostToolUse funcionan
correctamente cuando son invocados desde un cwd externo (como hace Claude Code en producción).

CONTEXTO
--------
En producción, Claude Code invoca `hooks/dispatch.py` como un proceso separado cuyo cwd
es el directorio del PROYECTO DEL USUARIO, no el de aris4u. Esto exige que:
  - sys.path.insert (basado en `__file__.resolve()`) resuelva los imports correctamente.
  - ARIS4U_ROOT (derivado de `Path(__file__).parents[2]`) use rutas absolutas.
  - Los handlers que escriben a logs/DB no dependan de `os.getcwd()` para sus paths.

NOTA sobre el model-routing-guard (Test 3)
------------------------------------------
La regla "Agent sin model= → exit 2" la materializa `~/.claude/hooks/model-routing-guard.py`,
que está registrado en `~/.claude/settings.json` como un hook PreToolUse SEPARADO (no dentro
de la cadena de `dispatch.py`). El dispatcher ARIS4U no incluye ese guard en su cadena interna
porque se evitaría la doble-comprobación del harness. Test 3 invoca ese guard directamente para
verificar que tampoco depende del cwd de ejecución.

Corre:  .venv312/bin/python3 -m pytest tests/integration/test_dispatch_subprocess.py -v --tb=short
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ARIS = Path(__file__).resolve().parents[2]
PY = ARIS / ".venv312" / "bin" / "python3"
DISPATCH = ARIS / "hooks" / "dispatch.py"
MODEL_GUARD = Path.home() / ".claude" / "hooks" / "model-routing-guard.py"


def _run_dispatch(
    event: str,
    payload: dict,
    cwd: str = "/tmp",
    timeout: int = 15,
) -> subprocess.CompletedProcess:
    """Invoca dispatch.py como proceso externo (replicando el uso real de Claude Code)."""
    return subprocess.run(
        [str(PY), str(DISPATCH), event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: PreToolUse Bash — cwd externo (/tmp)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
def test_pretooluse_bash_from_external_cwd() -> None:
    """PreToolUse Bash desde /tmp no debe crashear — fail-open (exit 0).

    Verifica que la cadena de handlers (migration_linter, phi_guard, screenshot_loop…)
    resuelve sus imports y sus rutas absolutas correctamente aunque el cwd sea externo.
    "echo hello" no activa ningún guard bloqueante.
    """
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
        "session_id": "pytest-external-cwd",
        "cwd": str(ARIS),  # cwd del proyecto (parte del payload Claude Code)
    }
    result = _run_dispatch("PreToolUse", payload, cwd="/tmp")

    assert result.returncode == 0, (
        f"PreToolUse Bash desde /tmp salió con {result.returncode}.\n"
        f"stderr: {result.stderr[:500]}"
    )
    assert "Traceback" not in result.stderr, (
        f"Crash detectado en PreToolUse Bash desde /tmp:\n{result.stderr[:800]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: PostToolUse Edit — cwd del proyecto de usuario (tmp_path)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
def test_posttooluse_edit_from_user_project_cwd(tmp_path: Path) -> None:
    """PostToolUse Edit desde el cwd de un proyecto de usuario no debe crashear.

    Simula el caso real: el usuario editó app.py; Claude Code invoca dispatch.py
    con cwd=proyecto-usuario. El code_quality_gate (solo .py) debe evaluar el path
    sin crashear aunque el archivo no exista (fail-open).
    """
    fake_file = tmp_path / "app.py"
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(fake_file),
            "old_string": "x = 1",
            "new_string": "x: int = 1",
        },
        "tool_output": "Edited successfully",
        "session_id": "pytest-external-cwd",
        "cwd": str(tmp_path),
    }
    result = _run_dispatch("PostToolUse", payload, cwd=str(tmp_path))

    assert result.returncode == 0, (
        f"PostToolUse Edit desde {tmp_path} salió con {result.returncode}.\n"
        f"stderr: {result.stderr[:500]}"
    )
    assert "Traceback" not in result.stderr, (
        f"Crash detectado en PostToolUse Edit desde {tmp_path}:\n{result.stderr[:800]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: model-routing-guard — Agent sin model= BLOQUEA desde cwd externo
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not MODEL_GUARD.exists(), reason="model-routing-guard.py no encontrado")
def test_model_routing_guard_blocks_agent_without_model_from_external_cwd() -> None:
    """El model-routing-guard bloquea (exit 2) desde /tmp aunque no sea el cwd de aris4u.

    NOTA DE DISEÑO: la regla "Agent sin model= → exit 2" la implementa el hook global
    `~/.claude/hooks/model-routing-guard.py` (registrado APARTE en settings.json, no
    dentro de la cadena dispatch.py). Este test lo invoca directamente — igual que lo
    hace Claude Code en producción — para confirmar que no depende del cwd de ejecución.

    El guard usa Path.home() para leer frontmatter de agentes: no depende de cwd.
    """
    payload = {
        "tool_name": "Agent",
        "tool_input": {"prompt": "hola"},  # sin model=, sin subagent_type con frontmatter fijo
    }
    result = subprocess.run(
        [sys.executable, str(MODEL_GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd="/tmp",
        timeout=10,
    )

    assert result.returncode == 2, (
        f"Esperaba exit 2 (guard bloquea), got {result.returncode}.\n"
        f"stderr: {result.stderr[:500]}\nstdout: {result.stdout[:200]}"
    )
    assert "GOBIERNO DE MODELOS" in result.stderr, (
        f"Mensaje de bloqueo no encontrado en stderr:\n{result.stderr[:500]}"
    )
    assert "Traceback" not in result.stderr, (
        f"El guard crashó en vez de bloquear limpiamente:\n{result.stderr[:800]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: UserPromptSubmit — cwd externo (/tmp)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not PY.exists(), reason="venv312 no disponible")
def test_userpromptsubmit_from_external_cwd() -> None:
    """UserPromptSubmit desde /tmp no debe crashear.

    Verifica que el router de capacidades y el recall se inicializan correctamente
    usando paths derivados de __file__ aunque el cwd sea externo. El handler puede
    emitir additionalContext (flujo) o no; lo que NO debe ocurrir es un Traceback.
    """
    payload = {
        "prompt": "ayuda con el código de producción",
        "session_id": "pytest-external-cwd",
        "cwd": str(ARIS),  # Claude Code envía el cwd del proyecto en el payload
        "hook_event_name": "UserPromptSubmit",
    }
    result = _run_dispatch("UserPromptSubmit", payload, cwd="/tmp")

    assert result.returncode == 0, (
        f"UserPromptSubmit desde /tmp salió con {result.returncode}.\n"
        f"stderr: {result.stderr[:500]}"
    )
    assert "Traceback" not in result.stderr, (
        f"Crash detectado en UserPromptSubmit desde /tmp:\n{result.stderr[:800]}"
    )
    # La salida (si existe) debe ser JSON válido — nunca texto suelto.
    out = result.stdout.strip()
    if out:
        try:
            json.loads(out)
        except json.JSONDecodeError as exc:
            pytest.fail(f"stdout no es JSON válido: {exc}\noutput: {out[:300]}")
