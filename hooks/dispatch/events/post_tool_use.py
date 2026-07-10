"""Handler PostToolUse — orquesta los hooks PostToolUse del repo aris4u.

A diferencia de los eventos 1:1 ya migrados, PostToolUse cablea VARIOS hooks del repo
(con matchers por tool). Este orquestador los corre todos respetando su matcher y
COMBINA su salida en una sola respuesta del contrato PostToolUse:

  - schema_drift            (Write/Edit/MultiEdit, async)  → telemetría + warn a stderr
  - redact_secrets          (Bash)                          → MUTA output (updatedToolOutput)
  - agent_dispatched        (Agent/Task, async)             → side-effect (JSONL HEAD snapshot)
  - parallel-dispatch-guard (Bash, .sh/.bash files)         → advisory additionalContext
  - capture_commit          (Bash, git commit)              → side-effect (decision en sessions.db)
  - code_quality_gate       (Write/Edit/MultiEdit .py)      → advisory ruff + complejidad
  - commit_quality_gate     (Bash, git commit)              → advisory pyright + tests afectados

PILAR DE VERIFICACIÓN (núcleo del producto, junto a memoria): para un developer que instala
ARIS4U, el sistema VERIFICA su código sin que lo pida. Cuatro piezas, todas fail-open y
distribuibles en hooks/ del plugin:
  1. code_quality_gate   — ruff + complejidad por-edit (aquí, PostToolUse).
  2. commit_quality_gate — pyright + tests afectados por-commit (aquí, PostToolUse).
  3. migration_linter    — gate de migraciones (PreToolUse, hooks/dispatch/handlers/).
  4. verify-gate         — recordatorio SUAVE al CERRAR (Stop): si se tocó código y nadie
                           verificó (señales recolectadas aquí vía tools.verify_gate).

NO se porta `post-change-ecosystem-validator.sh` (es GLOBAL en ~/.claude/hooks, fuera del
dispatcher del repo).

Contrato combinado: el harness corre UN solo hook por evento aquí, así que se emite UN
JSON que reúne `updatedToolOutput` (de redact) + `additionalContext` (redact note +
parallel-guard). Los warnings de schema_drift van a stderr (no bloqueante, exit 0). Los
side-effects (capture_commit, agent_dispatched) ocurren en silencio. Fail-open total.

La mutación de redact_secrets se emite EXACTA como el .sh viejo (decision:allow +
hookSpecificOutput.updatedToolOutput + additionalContext "Secret redaction: N …"),
preservando además el logging del evento secret_redacted a logs/v16.1-events.jsonl.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, UTC
from typing import NoReturn

from dispatch.contract import ARIS4U_ROOT
from dispatch.handlers import agent_dispatched as _agent_dispatched
from dispatch.handlers import capture_commit as _capture_commit
from dispatch.handlers import code_quality_gate as _code_quality_gate
from dispatch.handlers import commit_quality_gate as _commit_quality_gate
from dispatch.handlers import parallel_dispatch_guard as _parallel_guard
from dispatch.handlers import redact as _redact
from dispatch.handlers import schema_drift as _schema_drift


def _log_secret_redacted(total: int) -> None:
    """Replica el log del evento secret_redacted a logs/v16.1-events.jsonl (nunca el secreto)."""
    try:
        log_path = ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"
        event = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": "secret_redacted",
            "hook": "redact_secrets",
            "total_redacted": total,
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


def _emit(payload: dict) -> NoReturn:
    """Emite el JSON combinado de PostToolUse y sale 0 (advisory, nunca bloquea)."""
    print(json.dumps(payload))
    sys.exit(0)


def _safe(fn, *args):
    """Corre fn(*args) con fail-open total; devuelve su resultado o None ante cualquier error."""
    try:
        return fn(*args)
    except Exception:
        return None


def _record_adoption(tool_name: str, tool_input: dict) -> None:
    """Fase 4: si esta invocación satisface un hint pendiente, emite ``capability_adopted``.

    Side-effect puro (sin salida): cierra el lazo del enrutador midiendo el USO real de lo
    sugerido. Import perezoso (tools/ no está en sys.path de los handlers por defecto) y
    fail-open total — la telemetría de adopción nunca debe perturbar PostToolUse.
    """
    if str(ARIS4U_ROOT) not in sys.path:
        sys.path.insert(0, str(ARIS4U_ROOT))
    from tools.capability_adoption import record_tool_use

    record_tool_use(os.environ.get("ARIS4U_SESSION_ID", ""), tool_name, tool_input)


def _record_verify_signal(tool_name: str, tool_input: dict) -> None:
    """Verify-gate: registra si esta invocación tocó código o corrió una verificación.

    Alimenta las señales por-turno (code_touched / verify_ran) que el Stop-hook consulta para
    el recordatorio SUAVE de cierre. Side-effect puro, import perezoso y fail-open total — la
    señal de verificación nunca debe perturbar PostToolUse.
    """
    if str(ARIS4U_ROOT) not in sys.path:
        sys.path.insert(0, str(ARIS4U_ROOT))
    from tools import verify_gate

    verify_gate.record_tool(os.environ.get("ARIS4U_SESSION_ID", ""), tool_name, tool_input)


def handle(event_name: str, inp: dict) -> None:
    tool_name = inp.get("tool_name") or ""
    tool_input = inp.get("tool_input") or {}
    tool_output = inp.get("tool_output") or ""
    cwd = inp.get("cwd") or os.getcwd()

    additional_context_parts: list[str] = []
    updated_output = None  # type: ignore[assignment]

    # --- redact_secrets (Bash): muta output. Caso crítico. ---
    red = _safe(_redact.redact, tool_name, tool_output)
    if red is not None:
        redacted, total = red
        if redacted is not None and total > 0:
            updated_output = redacted
            _log_secret_redacted(total)
            additional_context_parts.append(f"Secret redaction: {total} credential(s) redacted")

    # --- advisories (cada handler devuelve texto para additionalContext, o None) ---
    for advisory in (
        _safe(_parallel_guard.check, tool_name, tool_input),         # .sh/.bash parallel-dispatch guard
        _safe(_code_quality_gate.run, tool_name, tool_input),        # .py edit: ruff + complejidad
        _safe(_commit_quality_gate.run, tool_name, tool_input, cwd),  # commit: pyright + tests afectados
    ):
        if advisory:
            additional_context_parts.append(advisory)

    # --- side-effects (sin salida) ---
    _safe(_capture_commit.run, tool_name, tool_input, cwd)           # commit → decision en sessions.db
    _safe(_agent_dispatched.run, tool_name, inp)                     # Agent/Task → JSONL snapshot
    _safe(_record_adoption, tool_name, tool_input)                   # Fase 4: hint→uso → capability_adopted
    _safe(_record_verify_signal, tool_name, tool_input)             # verify-gate: code_touched / verify_ran

    # --- schema_drift (Write/Edit/MultiEdit): warn a stderr ---
    warn = _safe(_schema_drift.run, tool_name, tool_input)
    if warn:
        print(warn, file=sys.stderr)

    # --- Salida combinada del contrato PostToolUse ---
    additional_context = "\n".join(additional_context_parts)

    if updated_output is not None:
        out: dict = {
            "decision": "allow",
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": updated_output,
            },
        }
        if additional_context:
            out["additionalContext"] = additional_context
        _emit(out)

    if additional_context:
        _emit(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": additional_context,
                }
            }
        )

    # Sin mutación ni contexto → no-op (exit 0).
    sys.exit(0)
