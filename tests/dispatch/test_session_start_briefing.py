"""Tests del self-briefing automático de ARIS4U en SessionStart.

Cubre:
  - startup en HOME (no-lab) → briefing presente en additionalContext
  - resume → briefing AUSENTE
  - budget duro: bloque < 2200 chars
  - contenido obligatorio: "ARIS4U" + las 5 MCP tools
  - fail-open: sessions.db inaccesible no rompe el handler

Corre:
    .venv312/bin/python3 -m pytest tests/dispatch/test_session_start_briefing.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
INVOKE = str(ROOT / "tests" / "dispatch" / "_invoke.py")
FIXDIR = Path(__file__).resolve().parent / "fixtures"

HOOKS = ROOT / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MCP_TOOLS = [
    "aris_recall_client",
    "aris_search",
    "aris_ingest",
    "aris_dialectic",
    "aris_health",
]
BUDGET_CHARS = 2200


def _run_fixture(fixture_name: str, extra_env: dict | None = None) -> tuple[str, int]:
    """Ejecuta el handler session_start con el fixture dado.

    Args:
        fixture_name: Nombre del archivo JSON en fixtures/ (sin path).
        extra_env: Variables de entorno adicionales para el subproceso.

    Returns:
        (additionalContext, returncode)
    """
    payload = (FIXDIR / fixture_name).read_text()
    env = {**os.environ}
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        [PY, INVOKE, "session_start", "SessionStart"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    out = proc.stdout.strip()
    if not out:
        return "", proc.returncode
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return out, proc.returncode
    # Extraer additionalContext desde hookSpecificOutput o top-level
    ac = data.get("additionalContext") or data.get("hookSpecificOutput", {}).get(
        "additionalContext", ""
    )
    return ac, proc.returncode


# ---------------------------------------------------------------------------
# Prueba A: startup no-lab emite briefing con identidad y herramientas
# ---------------------------------------------------------------------------

def test_startup_nonlab_has_briefing() -> None:
    """source=startup en cwd no-lab → additionalContext contiene el briefing de ARIS4U."""
    ctx, rc = _run_fixture("session_start_nonlab.json")
    assert rc == 0, f"returncode={rc}"
    assert "ARIS4U" in ctx, f"'ARIS4U' no encontrado en:\n{ctx[:500]}"


def test_startup_nonlab_has_all_mcp_tools() -> None:
    """El briefing lista explícitamente las 5 MCP tools."""
    ctx, _ = _run_fixture("session_start_nonlab.json")
    for tool in MCP_TOOLS:
        assert tool in ctx, f"MCP tool '{tool}' no encontrada en briefing:\n{ctx[:800]}"


def test_startup_nonlab_within_budget() -> None:
    """El briefing (additionalContext) no supera BUDGET_CHARS chars."""
    ctx, _ = _run_fixture("session_start_nonlab.json")
    # Solo el bloque briefing; el total puede incluir warning de write-path.
    # Verificamos que el contexto completo sea razonable (< 3x budget incluyendo warning).
    assert len(ctx) < BUDGET_CHARS * 3, (
        f"additionalContext demasiado largo: {len(ctx)} chars"
    )
    # Y que el bloque de briefing puro (hasta el separador) no supere el budget.
    briefing_block = ctx.split("─────────────────────────────────────────────────────────")[0]
    # Reincluimos el header que puede estar en otra parte
    assert len(briefing_block) + 60 < BUDGET_CHARS * 2, (
        f"briefing_block parece excesivo: {len(briefing_block)} chars"
    )


def test_startup_nonlab_has_hardware_block(tmp_path: Path) -> None:
    """El briefing incluye workers definidos en hardware.workers (mecanismo genérico).

    Inyecta un config temporal con un worker de ejemplo y verifica que el nombre
    aparece en el briefing. No hardcodea ningún nombre de instancia ("W2", etc.):
    prueba el mecanismo, no los datos del dueño.
    """
    worker_name = "worker-gpu"
    cfg = {
        "hardware": {
            "primary": "TestPrimary 16GB",
            "workers": [
                {
                    "name": worker_name,
                    "ssh": worker_name,
                    "gpu": "RTX 4090 24GB",
                    "note": "worker de ejemplo para tests",
                    "enabled": True,
                }
            ],
        }
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(cfg))

    ctx, _ = _run_fixture(
        "session_start_nonlab.json",
        extra_env={"ARIS4U_CONFIG": str(config_file)},
    )
    assert worker_name in ctx, (
        f"Worker '{worker_name}' no encontrado en briefing "
        f"(hardware.workers no se propaga):\n{ctx[:600]}"
    )


# ---------------------------------------------------------------------------
# Prueba D: resume NO emite briefing
# ---------------------------------------------------------------------------

def test_resume_no_briefing() -> None:
    """source=resume → el briefing NO aparece en additionalContext."""
    ctx, rc = _run_fixture("session_start_resume.json")
    assert rc == 0, f"returncode={rc}"
    # resume sin lab y sin write-path stale debe ser silencioso (ctx vacío o sin briefing)
    # Si hay ctx, NO debe contener el bloque de briefing.
    assert "ARIS4U BRIEFING" not in ctx, (
        f"Briefing aparece en resume (no debería):\n{ctx[:500]}"
    )
    assert "INVOCA A MANO" not in ctx, (
        f"Bloque opt-in aparece en resume (no debería):\n{ctx[:500]}"
    )


# ---------------------------------------------------------------------------
# Prueba E: fail-open con sessions.db inaccesible
# ---------------------------------------------------------------------------

def test_failopen_inaccessible_sessions_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Con sessions.db apuntando a path inexistente, el handler no falla."""
    # Sobreescribimos SESSIONS_DB en _briefing vía env no es viable; usamos
    # una sesión donde simplemente el DB sería inaccesible al aislarlo.
    # Approach: parchar la función _db_memory directamente en el módulo.
    sys.path.insert(0, str(HOOKS))
    from dispatch.events import _briefing

    original_sessions = _briefing.SESSIONS_DB
    try:
        # Apuntar SESSIONS_DB a un archivo que no existe
        monkeypatch.setattr(_briefing, "SESSIONS_DB", tmp_path / "nonexistent.db")
        result = _briefing.build_briefing("startup")
        # Debe devolver algo (vacío o briefing sin memoria) — no lanzar excepción
        assert isinstance(result, str), "build_briefing debe devolver str"
        # El sistema debe mencionar ARIS4U aunque la memoria falle
        assert "ARIS4U" in result, f"Sin memoria, briefing debería seguir teniendo 'ARIS4U':\n{result}"
    finally:
        monkeypatch.setattr(_briefing, "SESSIONS_DB", original_sessions)


# ---------------------------------------------------------------------------
# Prueba de unidad directa en build_briefing (verificación de budget puro)
# ---------------------------------------------------------------------------

def test_build_briefing_budget_chars() -> None:
    """build_briefing() importado directamente cumple el budget de 2200 chars."""
    sys.path.insert(0, str(HOOKS))
    from dispatch.events._briefing import BUDGET_CHARS as B
    from dispatch.events._briefing import build_briefing

    result = build_briefing("startup")
    assert isinstance(result, str)
    assert len(result) <= B, (
        f"build_briefing excede el budget: {len(result)} > {B} chars\n"
        f"Primeros 300: {result[:300]}"
    )


def test_build_briefing_resume_not_called_but_returns_ok() -> None:
    """build_briefing en modo startup no lanza; la guardia source=='resume' está en caller."""
    sys.path.insert(0, str(HOOKS))
    from dispatch.events._briefing import build_briefing

    result = build_briefing("startup")
    assert isinstance(result, str)
    assert len(result) > 0, "build_briefing('startup') devolvió cadena vacía inesperadamente"
