"""Contrato de E/S de los hooks de Claude Code — dispatcher ARIS4U V2.

Dos salidas que entiende el harness de Claude Code:
  - exit 0 + stdout JSON  → advisory (additionalContext, no bloquea)
  - exit 2 + stderr        → bloqueo (el mensaje va al modelo)

Resuelve ARIS4U_ROOT una sola vez (mata los $HOME hardcodeados en los .sh viejos).
Todas las funciones de salida hacen sys.exit; el dispatcher captura SystemExit.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import NoReturn

# Raíz del repo: CLAUDE_PLUGIN_ROOT (modo plugin) o derivada del path de este archivo
# (hooks/dispatch/contract.py → parents[2] = raíz del repo aris4u).
ARIS4U_ROOT: Path = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parents[2])


def read_event() -> dict:
    """Lee el payload JSON del evento desde stdin. Fail-open a {} si vacío/inválido."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def passthrough() -> NoReturn:
    """No-op: exit 0 sin salida (el tool sigue su curso normal)."""
    sys.exit(0)


def emit_additional_context(context: str) -> NoReturn:
    """Formato legacy {additionalContext}: usado por depth_inject / subagent_depth."""
    if not context:
        sys.exit(0)
    print(json.dumps({"additionalContext": context}))
    sys.exit(0)


def advise(context: str, event_name: str) -> NoReturn:
    """Formato hookSpecificOutput: usado por los guards PreToolUse (advisory)."""
    if not context:
        sys.exit(0)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}}))
    sys.exit(0)


def block(message: str) -> NoReturn:
    """Bloqueo: stderr + exit 2 (equivalente al exit 2 de migration_linter / phi_guard)."""
    print(message, file=sys.stderr)
    sys.exit(2)
