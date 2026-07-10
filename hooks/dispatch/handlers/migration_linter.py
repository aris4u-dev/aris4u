"""Handler PreToolUse — migration_linter (BLOQUEANTE, exit 2).

Porta `hooks/migration_linter.sh` 1:1. Detecta el stack de migraciones del comando
Bash, localiza el directorio de migraciones por convención (relativo al cwd del evento),
corre `tools/migration_linter.py` (que YA trae el fix de FP en cuerpos $$...$$ plpgsql,
ver `_check_forward_table_references`) y, si hay errores → BLOCK(msg) (== exit 2 del .sh).

Pura: no hace sys.exit; devuelve Verdict. Fail-open: cualquier error de infra → PASS.
El mensaje de bloqueo replica EXACTO el here-doc del .sh ("🚫 Migration Lint Failed …").
"""
from __future__ import annotations

import contextlib
import io
import os
from datetime import datetime, UTC
from pathlib import Path
from typing import Optional

from dispatch.contract import ARIS4U_ROOT
from dispatch.handlers import verdict as V

# Mismo case-detection que el .sh (orden preservado).
_STACK_SIGNALS = [
    ("supabase", ("supabase db push", "supabase migration up", "supabase db reset")),
    ("flyway", ("mvn flyway:migrate", "mvn flyway:repair",
                "./mvnw flyway:migrate", "./mvnw flyway:repair",
                "flyway migrate", "flyway -url")),
    ("prisma", ("prisma migrate deploy", "prisma migrate dev")),
    ("alembic", ("alembic upgrade",)),
]


def _detect_stack(cmd: str) -> str:
    for stack, needles in _STACK_SIGNALS:
        for n in needles:
            if n in cmd:
                return stack
    return ""


def _locate_migrations_dir(stack: str, cmd: str, cwd: str) -> Optional[str]:
    """Replica la localización por convención de stack del .sh, relativa al cwd."""
    base = Path(cwd)
    if stack == "supabase":
        if "/supabase/" in cmd:
            for tok in cmd.split():
                if "supabase/migrations" in tok:
                    return str(Path(tok).parent)
        return str(base / "supabase" / "migrations")
    if stack == "flyway":
        default = base / "src" / "main" / "resources" / "db" / "migration"
        if default.is_dir():
            return str(default)
        for cand in base.glob("**/src/main/resources/db/migration"):
            if cand.is_dir():
                return str(cand)
        return None
    if stack == "prisma":
        return str(base / "prisma" / "migrations")
    if stack == "alembic":
        for cand in (base / "alembic" / "versions", base / "migrations" / "versions"):
            if cand.is_dir():
                return str(cand)
        return None
    return None


def _emit_event(payload: dict) -> None:
    """Replica el emit_event JSONL del .sh (opt-in vía ARIS4U_VALIDATION_LOG/LOG_FILE)."""
    log = os.environ.get("ARIS4U_LOG_FILE")
    if not os.environ.get("ARIS4U_VALIDATION_LOG") or not log:
        return
    with contextlib.suppress(Exception):
        import json
        with open(log, "a") as f:
            f.write(json.dumps(payload) + "\n")


def _emit_main_block_event(payload: dict) -> None:
    """Escribe un evento de bloqueo en el log principal v16.1-events.jsonl.

    A diferencia de _emit_event (solo activo en modo validación), este siempre
    escribe al log principal cuando el handler está activo — igual que phi_guard.
    Permite que guard_blocks sea contable por session_id (Batch O).
    """
    import json
    log_file = ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"
    if not log_file.parent.is_dir():
        return
    with contextlib.suppress(Exception):
        with open(log_file, "a") as f:
            f.write(json.dumps(payload) + "\n")


def _run_linter(migrations_dir: str) -> tuple[int, str, int]:
    """Corre el linter en-proceso. Devuelve (exit_code, output_jsonl, error_count)."""
    import sys

    linter_root = str(ARIS4U_ROOT)
    tools_dir = str(ARIS4U_ROOT / "tools")
    for p in (linter_root, tools_dir):
        if p not in sys.path:
            sys.path.insert(0, p)

    from tools.migration_linter import MigrationLinter  # type: ignore

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        linter = MigrationLinter(naming="auto")
        exit_code = linter.lint_path(migrations_dir)
    output = buf.getvalue()
    error_count = sum(1 for f in linter.findings if f.severity == "error")
    return exit_code, output, error_count


def check(tool_name: str, tool_input: dict, cwd: str) -> V.Verdict:
    """Veredicto del migration_linter. BLOCK si la migración tiene errores."""
    if tool_name != "Bash":
        return V.ok()
    cmd = (tool_input or {}).get("command") or ""
    if not cmd:
        return V.ok()

    stack = _detect_stack(cmd)
    if not stack:
        return V.ok()

    ts = datetime.now(UTC).isoformat()
    migrations_dir = _locate_migrations_dir(stack, cmd, cwd or os.getcwd())

    if not migrations_dir or not Path(migrations_dir).is_dir():
        _emit_event({
            "ts": ts, "hook": "migration_linter", "event": "linter_skipped",
            "reason": "no migrations dir", "stack": stack,
            "migrations_dir": (migrations_dir or "")[:200],
        })
        return V.ok()

    linter_path = ARIS4U_ROOT / "tools" / "migration_linter.py"
    if not linter_path.is_file():
        return V.ok()

    try:
        exit_code, output, error_count = _run_linter(migrations_dir)
    except Exception:
        # Fail-open: el linter explotó → NO bloquear (igual que el .sh ante error de infra).
        return V.ok()

    warn_count = output.count('"severity": "warning"')
    _emit_event({
        "ts": ts, "hook": "migration_linter",
        "event": "migration_lint_" + ("blocked" if exit_code != 0 else "passed"),
        "stack": stack, "migrations_dir": migrations_dir[:200],
        "exit": exit_code, "errors": error_count, "warnings": warn_count,
    })

    if exit_code != 0 or error_count > 0:
        # Emit to main events log (always, unlike _emit_event) so guard_blocks is
        # countable by session_id in _read_session_signals_from_log (Batch O).
        _emit_main_block_event({
            "ts": ts, "hook": "migration_linter",
            "event": "migration_lint_blocked",
            "stack": stack,
            "session_id": os.environ.get("ARIS4U_SESSION_ID", ""),
        })
        msg = (
            "🚫 Migration Lint Failed — Supabase apply blocked\n\n"
            f"  Migrations dir: {migrations_dir}\n"
            f"  Errors: {error_count}\n"
            f"  Warnings: {warn_count}\n\n"
            "  Details:\n"
            f"{output}\n"
            "  Fix the errors above before running:\n"
            f"    {cmd}"
        )
        return V.block(msg)

    return V.ok()
