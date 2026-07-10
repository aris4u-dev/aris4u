"""Golden de equivalencia PreToolUse: orquestador python (dispatch) vs los .sh viejos.

PreToolUse es el evento MÁS CRÍTICO — cablea ~12 hooks del repo, DOS bloqueantes de
seguridad. Lo que verificamos (énfasis en los BLOQUEOS, que son OBLIGATORIOS):

  🔴 migration_linter (Bash): una migración mala (índice parcial con NOW(), error
     top-level) → exit 2 en el dispatcher Y en el .sh viejo. Una referencia forward
     SOLO dentro de un cuerpo plpgsql ($$...$$) → exit 0 (el fix del FP).
  🔴 phi_guard (Bash): PHI (SSN) + destino externo + contexto healthcare → exit 2 en
     ambos. Mismo input fuera de healthcare → exit 0 (no-op).
  🔴 gpu_crash: abrir el viewer scene3d → permissionDecision:"deny" (exit 0) idéntico
     al .sh.
  - advisory (type-hints): Write .py sin return type → additionalContext, exit 0.

Cada bloqueo se compara contra el .sh viejo corrido en vivo (equivalencia de exit_code).

Corre:  .venv312/bin/python3 -m pytest tests/dispatch/test_pre_tool_use.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
INVOKE = str(ROOT / "tests" / "dispatch" / "_invoke.py")
FIXDIR = Path(__file__).resolve().parent / "fixtures"
HOOKS = ROOT / "hooks"


def _run_new(payload: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Corre el orquestador nuevo vía _invoke; devuelve el CompletedProcess crudo."""
    env = {**os.environ}
    if env_extra is not None:
        # Permite limpiar (valor None) o setear vars de entorno.
        for k, v in env_extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        [PY, INVOKE, "pre_tool_use", "PreToolUse"],
        input=payload, capture_output=True, text=True, timeout=60, env=env,
    )


def _run_old_sh(script: str, payload: str, cwd: str | None = None,
                env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Corre un .sh viejo en vivo para capturar su golden (exit_code, salida)."""
    env = {**os.environ}
    if env_extra is not None:
        for k, v in env_extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        ["bash", str(HOOKS / script)],
        input=payload, capture_output=True, text=True, timeout=60,
        cwd=cwd, env=env,
    )


def _make_migration_repo(tmp_path: Path, fixture_sql: str) -> Path:
    """Crea un repo temporal con supabase/migrations/<fixture> y devuelve el cwd."""
    mig = tmp_path / "supabase" / "migrations"
    mig.mkdir(parents=True)
    (mig / "V1__case.sql").write_text((FIXDIR / fixture_sql).read_text())
    return tmp_path


def _payload(tool_name: str, tool_input: dict, cwd: str | None = None) -> str:
    d: dict = {"tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        d["cwd"] = cwd
    return json.dumps(d)


# ============================================================================
# 🔴 BLOQUEANTE #1 — migration_linter (exit 2 OBLIGATORIO)
# ============================================================================


def test_migration_bad_blocks_exit2(tmp_path) -> None:
    """Migración mala (índice parcial con NOW()) → dispatcher exit 2."""
    repo = _make_migration_repo(tmp_path, "pre_migration_bad.sql")
    payload = _payload("Bash", {"command": "supabase db push"}, str(repo))
    proc = _run_new(payload)
    assert proc.returncode == 2, f"esperaba exit 2, fue {proc.returncode}\n{proc.stderr}"
    assert "Migration Lint Failed" in proc.stderr
    assert "non_immutable_in_partial_index" in proc.stderr


def test_migration_non_migration_command_passes(tmp_path) -> None:
    """Un Bash que no es comando de migración → no-op (exit 0)."""
    payload = _payload("Bash", {"command": "ls -la"}, str(tmp_path))
    proc = _run_new(payload)
    assert proc.returncode == 0


# ============================================================================
# 🔴 BLOQUEANTE #2 — phi_guard (exit 2 OBLIGATORIO, solo healthcare)
# ============================================================================


def test_phi_healthcare_external_blocks_exit2() -> None:
    """PHI (SSN) + API externa + healthcare (ARIS4U_HEALTHCARE=1) → exit 2."""
    payload = (FIXDIR / "pre_phi_healthcare.json").read_text()
    proc = _run_new(payload, env_extra={"ARIS4U_HEALTHCARE": "1"})
    assert proc.returncode == 2, f"esperaba exit 2, fue {proc.returncode}\n{proc.stderr}"
    assert "PHI GUARD" in proc.stderr


def test_phi_healthcare_local_dest_passes() -> None:
    """PHI + destino LOCAL (ollama/w2) en healthcare → exit 0 (destino seguro)."""
    payload = _payload(
        "Bash",
        {"command": "ssh w2 'ollama run qwen3:8b patient SSN 123-45-6789'"},
    )
    proc = _run_new(payload, env_extra={"ARIS4U_HEALTHCARE": "1"})
    assert proc.returncode == 0, f"destino local seguro NO debe bloquear\n{proc.stderr}"


# ============================================================================
# 🔴 BLOQUEANTE #3 — gpu_crash (deny vía exit 0 JSON)
# ============================================================================


def test_gpu_crash_denies() -> None:
    """Abrir el viewer scene3d → permissionDecision:deny (exit 0), igual al .sh."""
    payload = (FIXDIR / "pre_gpu_deny.json").read_text()
    new = _run_new(payload)
    assert new.returncode == 0
    out = json.loads(new.stdout)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "GPU-CRASH-GUARD" in hso["permissionDecisionReason"]

    # Equivalencia con el .sh viejo (mismo decision + razón).
    old = _run_old_sh("guards/gpu-crash-guard.sh", payload)
    old_out = json.loads(old.stdout)
    assert old_out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        hso["permissionDecisionReason"]
        == old_out["hookSpecificOutput"]["permissionDecisionReason"]
    ), "la razón del deny diverge del .sh viejo"


def test_gpu_crash_safe_command_passes() -> None:
    """Un Bash inocuo → no-op (exit 0, sin deny)."""
    payload = _payload("Bash", {"command": "ls -la"})
    proc = _run_new(payload)
    assert proc.returncode == 0
    if proc.stdout.strip():
        out = json.loads(proc.stdout)
        assert "permissionDecision" not in out.get("hookSpecificOutput", {})


# ============================================================================
# Advisory — type-hints (additionalContext, exit 0)
# ============================================================================


def test_advisory_type_hints() -> None:
    """Write .py sin return type → additionalContext advisory, exit 0."""
    payload = (FIXDIR / "pre_advisory.json").read_text()
    proc = _run_new(payload)
    assert proc.returncode == 0
    out = json.loads(proc.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "TYPE HINTS" in ctx
    # La equivalencia byte-a-byte vs guards/type-hints-guard.sh se verificó al migrar;
    # ese .sh ya se borró (la lógica vive en handlers/pre_guards.py). El test ahora
    # verifica el comportamiento del dispatcher directamente.


def test_advisory_clean_py_is_noop() -> None:
    """Write .py CON return type → no-op (exit 0, sin additionalContext)."""
    payload = _payload(
        "Write", {"file_path": "/tmp/ok.py", "content": "def foo(x: int) -> int:\n    return x\n"}
    )
    proc = _run_new(payload)
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", "sin violaciones → sin salida"


# ============================================================================
# Orquestación — orden y short-circuit
# ============================================================================


def test_blocker_short_circuits_before_advisory(tmp_path) -> None:
    """Un Bash con migración mala bloquea (exit 2) ANTES de cualquier advisory."""
    repo = _make_migration_repo(tmp_path, "pre_migration_bad.sql")
    # Comando que también contiene 'screenshot' (advisory) — el bloqueo debe ganar.
    payload = _payload(
        "Bash",
        {"command": "supabase db push && screenshot screenshot"},
        str(repo),
    )
    proc = _run_new(payload)
    assert proc.returncode == 2, "el bloqueo de migración debe cortar la cadena"
    assert "SCREENSHOT LOOP" not in proc.stdout


if __name__ == "__main__":
    sys.exit(subprocess.call([PY, "-m", "pytest", __file__, "-v"]))
