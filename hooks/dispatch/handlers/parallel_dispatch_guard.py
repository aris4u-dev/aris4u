"""Handler parallel-dispatch-guard — portado de hooks/guards/parallel-dispatch-guard.sh.

PostToolUse(Write/Edit) sobre scripts .sh/.bash: detecta llamadas `ssh w[0-9]`
secuenciales (sin `&` final) y sugiere paralelizar (parallel-dispatch.md).
Advisory puro (no bloquea). Devuelve el string additionalContext (vacío si no aplica).
"""
from __future__ import annotations

import re

_SSH_W = re.compile(r"ssh\s+w[0-9]")
_TRAILING_AMP = re.compile(r"&\s*$")
_BLOCKING = re.compile(r"^\s*(wait|if|for|while)\b")


def check(tool_name: str, tool_input: dict) -> str:
    """Devuelve el mensaje additionalContext (o "" si no hay violaciones).

    Args:
        tool_name: nombre del tool (no se filtra aquí; el filtro real es la extensión).
        tool_input: tool_input del evento (usa file_path + content).

    Returns:
        Mensaje de advertencia para additionalContext, o "" si no aplica.
    """
    file_path = (tool_input or {}).get("file_path") or "unknown.sh"
    new_content = (tool_input or {}).get("content") or ""

    # Solo scripts bash/sh.
    if not re.search(r"\.(sh|bash)$", file_path):
        return ""

    violations = 0
    for line in new_content.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s*#", line):
            continue
        if _SSH_W.search(line):
            if not _TRAILING_AMP.search(line):
                violations += 1
        if _BLOCKING.match(line):
            if violations > 0:
                break

    if violations > 0:
        return (
            f"⚠️ PARALLEL DISPATCH: {violations} sequential ssh call(s) could be "
            "parallelized. Use 'ssh w2 cmd &' pattern for ~50% faster execution"
        )
    return ""
