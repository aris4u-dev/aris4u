"""Utilidades compartidas por los handlers PreToolUse del dispatcher ARIS4U.

Reúne el parseo de tool_input que cada .sh viejo hacía por su cuenta (con un
sub-proceso python embebido). Aquí se hace UNA vez, en-proceso, y se reparte a los
handlers puros. Todo es fail-open: ante cualquier error → estructuras vacías.
"""
from __future__ import annotations

from typing import Any, List


def walk_strings(value: Any) -> List[str]:
    """Aplana recursivamente todos los strings de un valor (dict/list/str).

    Replica el `walk()` de phi_guard.sh / phi_sanitizer.sh: concatena cada string
    de tool_input para el matching de patrones.
    """
    out: List[str] = []

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for vv in v.values():
                _walk(vv)
        elif isinstance(v, list):
            for vv in v:
                _walk(vv)

    _walk(value)
    return out


def tool_text(tool_input: dict) -> str:
    """Texto concatenado de todos los strings de tool_input (para regex PHI)."""
    return " ".join(walk_strings(tool_input or {}))


def matches_tool(matcher: str, tool_name: str) -> bool:
    """Emula el matcher de settings.json (`A|B|C`) contra un tool_name."""
    if not matcher:
        return True
    return tool_name in matcher.split("|")
