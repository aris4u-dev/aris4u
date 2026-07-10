"""Handler capture_commit — portado de hooks/capture_commit.sh (PostToolUse Bash).

Captura cada `git commit` como una decision con su client_id, automáticamente
(cierra el gap de captura 100% manual vía aris_ingest). Idempotente por session_ref
(el sha corto). Side-effect puro: escribe en sessions.db; no emite nada a stdout.

Equivalencia preservada: gating por `git commit` en el comando, cwd del evento,
resolución de client_id por path, dedup por session_ref.
"""
from __future__ import annotations

import os
import subprocess
import sys

from dispatch.contract import ARIS4U_ROOT


def run(tool_name: str, tool_input: dict, cwd: str) -> None:
    """Captura el último commit del repo en cwd como decision. No-op si no aplica.

    Args:
        tool_name: nombre del tool (solo "Bash").
        tool_input: tool_input del evento (usa command).
        cwd: directorio de trabajo del evento.
    """
    if tool_name != "Bash":
        return
    command = (tool_input or {}).get("command") or ""
    if "git commit" not in command:
        return

    repo_cwd = cwd or os.getcwd()

    if str(ARIS4U_ROOT) not in sys.path:
        sys.path.insert(0, str(ARIS4U_ROOT))

    try:
        sha = subprocess.run(
            ["git", "-C", repo_cwd, "log", "-1", "--format=%h"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        msg = subprocess.run(
            ["git", "-C", repo_cwd, "log", "-1", "--format=%s%n%b"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not sha or not msg:
            return
        from engine.v16.session_manager import (
            query_db,
            resolve_client_from_path,
            save_decision,
        )

        # Idempotencia: no duplicar si este commit ya fue capturado.
        if query_db(
            "SELECT 1 FROM decisions WHERE session_ref = ? LIMIT 1",
            (sha,),
            fetch_all=False,
        ):
            return
        save_decision(
            decision=f"[commit {sha}] {msg.splitlines()[0][:200]}",
            rationale=msg[:500],
            domain="git-commit",
            session_ref=sha,
            client_id=resolve_client_from_path(repo_cwd),
            mem_type="provenance",  # trazabilidad, NO guía de recall (ambos canales lo excluyen)
        )
    except Exception:
        return
