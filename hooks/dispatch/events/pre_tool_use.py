"""Handler PreToolUse — orquesta TODOS los hooks PreToolUse del repo aris4u.

PreToolUse es el evento MÁS CRÍTICO: cablea ~12 hooks del repo, DOS de ellos BLOQUEANTES
de seguridad. Este orquestador los corre en el MISMO orden del array `PreToolUse` de
`~/.claude/settings.json`, respetando el matcher por `tool_name` de cada uno.

Hooks portados (orden = settings.json; `pre-bash-guard.sh` queda FUERA, es GLOBAL):
  1. f5_prevalidation       (Write|Edit)               advisory (shadow)
  2. migration_linter       (Bash)                     🔴 BLOQUEANTE (exit 2)
  3. phi_guard              (Bash|WebFetch|WebSearch)  🔴 BLOQUEANTE (exit 2, healthcare)
  4. phi_sanitizer          (Bash|Write|Edit|Read)     advisory (healthcare)
  4b. mcp_guard             (mcp__*)                   advisory (telemetría + riesgo MCP)
  5. type-hints             (Write|Edit)               advisory
  6. docker-latest          (Write|Edit)               advisory
  7. supabase-rls           (Write|Edit)               advisory
  8. spring-boot-pattern    (Write|Edit)               advisory
  9. screenshot-loop        (Bash)                     advisory
 10. kb-docs-validator      (Write|Edit)               advisory
 11. gpu-crash              (Bash|playwright navigate) 🔴 BLOQUEANTE (deny, exit 0)

REGLA DE ORO de orquestación:
  - Se ejecutan EN ORDEN. El PRIMER veredicto de bloqueo (BLOCK exit 2 / DENY deny) corta
    la cadena de inmediato y NO se siguen ejecutando los demás.
  - Los advisory acumulan additionalContext y se emiten JUNTOS al final (un solo JSON
    hookSpecificOutput.additionalContext, exit 0).
  - Cada handler es una función pura `(inp) -> Verdict`. Si un handler crashea (error de
    infra) → se ignora (fail-open) y la cadena continúa. Un bloqueante que falla NO bloquea.
"""
from __future__ import annotations

import json
import os
import sys
from typing import List, NoReturn, Tuple
from collections.abc import Callable

from dispatch.contract import block as _contract_block
from dispatch.handlers import f5_prevalidation as _f5
from dispatch.handlers import mcp_guard as _mcp_guard
from dispatch.handlers import migration_linter as _migration
from dispatch.handlers import phi_guard as _phi_guard
from dispatch.handlers import phi_sanitizer as _phi_sanitizer
from dispatch.handlers import pre_guards as _guards
from dispatch.handlers import verdict as V
from datetime import UTC


def _emit_deny(reason: str) -> NoReturn:
    """Emite permissionDecision:deny (exit 0) — equivalente a gpu-crash-guard.sh."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


# Handlers cuyo fallo NO debe ser MUDO: si un guard de SEGURIDAD se degrada a
# fail-open, hay que dejar rastro (un guard apagado en silencio es peor que ruidoso).
_BLOCKERS = {"migration_linter", "phi_guard"}


def _log_degraded(name: str, exc: Exception) -> None:
    """Un guard bloqueante degradado a fail-open deja rastro (stderr SIEMPRE + event log best-effort).

    No cambia el contrato fail-open (la cadena sigue, no bloquea); solo lo hace
    observable para que un guard apagado por un bug no pase inadvertido.
    """
    msg = (
        f"⚠️ ARIS4U: guard BLOQUEANTE '{name}' se DEGRADÓ a fail-open por error "
        f"({type(exc).__name__}: {exc}). La validación NO corrió en esta invocación."
    )
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass
    try:
        from dispatch.contract import ARIS4U_ROOT
        from datetime import datetime, timezone

        log = ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"
        log.parent.mkdir(exist_ok=True)
        with open(log, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now(UTC).isoformat(),
                "hook": name, "event": "guard_degraded_failopen",
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }) + "\n")
    except Exception:
        pass


def _emit_advisory(parts: List[str]) -> NoReturn:
    """Emite el additionalContext combinado (exit 0) o no-op si está vacío."""
    context = "\n".join(p for p in parts if p)
    if not context:
        sys.exit(0)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": context,
        }
    }))
    sys.exit(0)


def handle(event_name: str, inp: dict) -> None:
    tool_name = inp.get("tool_name") or ""
    tool_input = inp.get("tool_input") or {}
    cwd = inp.get("cwd") or os.getcwd()

    # Cadena en el ORDEN EXACTO de settings.json. Cada entry: callable -> Verdict.
    # Los handlers de seguridad reciben cwd; los puros de contenido, no.
    chain: List[Tuple[str, Callable[[], V.Verdict]]] = [
        ("f5_prevalidation", lambda: _f5.check(tool_name, tool_input)),
        ("migration_linter", lambda: _migration.check(tool_name, tool_input, cwd)),
        ("phi_guard", lambda: _phi_guard.check(tool_name, tool_input, cwd)),
        ("phi_sanitizer", lambda: _phi_sanitizer.check(tool_name, tool_input, cwd)),
        ("mcp_guard", lambda: _mcp_guard.check(tool_name, tool_input, cwd)),
        ("type_hints", lambda: _guards.type_hints(tool_name, tool_input)),
        ("docker_latest", lambda: _guards.docker_latest(tool_name, tool_input)),
        ("supabase_rls", lambda: _guards.supabase_rls(tool_name, tool_input)),
        ("spring_boot", lambda: _guards.spring_boot(tool_name, tool_input)),
        ("screenshot_loop", lambda: _guards.screenshot_loop(tool_name, tool_input)),
        ("kb_docs", lambda: _guards.kb_docs(tool_name, tool_input)),
        ("gpu_crash", lambda: _guards.gpu_crash(tool_name, tool_input)),
    ]

    advisories: List[str] = []
    for _name, fn in chain:
        try:
            verdict = fn()
        except Exception as e:
            # Fail-open: un handler que crashea NO bloquea ni rompe la cadena.
            # PERO un BLOQUEANTE de seguridad degradado NO debe ser mudo (WS-G):
            # deja rastro para no apagar un guard sin que nadie se entere.
            if _name in _BLOCKERS:
                _log_degraded(_name, e)
            continue

        if verdict.kind == V.BLOCK:
            # Bloqueo duro: stderr + exit 2 (corta la cadena).
            _contract_block(verdict.text)
        if verdict.kind == V.DENY:
            # Deny (gpu-crash): JSON permissionDecision:deny + exit 0 (corta la cadena).
            _emit_deny(verdict.text)
        if verdict.kind == V.ADVISE and verdict.text:
            advisories.append(verdict.text)

    # Sin bloqueo → emitir advisorios acumulados (o no-op).
    _emit_advisory(advisories)
