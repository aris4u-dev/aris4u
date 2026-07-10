"""Handler agent_dispatched — portado de hooks/agent_dispatched.sh (PostToolUse Agent|Task).

Registra el dispatch de un subagente + snapshot del git HEAD de los repos-lab, para
que el post_agent_verify (Stop) tenga ventana de diff real. Side-effect puro: escribe
una línea JSONL en ARIS4U_LOG_FILE; no emite nada a stdout. Solo corre si el
validation log está activo (ARIS4U_VALIDATION_LOG + ARIS4U_LOG_FILE), igual que el .sh.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone, UTC

# Vacío por defecto — config ausente ⇒ sin repos-lab para snapshot de git HEAD.
# Para activar, configura "lab_projects" en ~/.aris4u/config.json:
#   {"lab_projects": [{"path": "/home/user/projects/mi-proyecto"}]}
_DEFAULT_LAB_REPOS: list[str] = []


def _lab_repos() -> list[str]:
    """Devuelve la lista de repos-lab desde config o el default exacto (fail-open).

    Config: ~/.aris4u/config.json campo "lab_projects" (lista de dicts con "path" str).
    Si no configurado o falla, devuelve _DEFAULT_LAB_REPOS sin cambios.

    Returns:
        Lista de paths (con '~' sin expandir en el default; expandidos si vienen de config).
    """
    try:
        import json as _json

        cfg_path = os.environ.get("ARIS4U_CONFIG") or str(
            os.path.join(os.path.expanduser("~"), ".aris4u", "config.json")
        )
        if os.path.isfile(cfg_path):
            cfg = _json.loads(open(cfg_path).read())
            projects = cfg.get("lab_projects")
            if projects:
                paths: list[str] = []
                for item in projects:
                    if isinstance(item, dict):
                        raw = item.get("path", "")
                    else:
                        raw = str(item)
                    raw = str(raw).rstrip("/")
                    if raw:
                        paths.append(raw)
                if paths:
                    return paths
    except Exception:
        pass
    return list(_DEFAULT_LAB_REPOS)


def run(tool_name: str, inp: dict) -> None:
    """Emite el evento agent_dispatched con repo_heads_pre. No-op si no aplica.

    Args:
        tool_name: nombre del tool (solo "Agent"/"Task").
        inp: payload completo del evento (usa tool_input.subagent_type/prompt/description).
    """
    if tool_name not in ("Agent", "Task"):
        return
    log_file = os.environ.get("ARIS4U_LOG_FILE")
    if not os.environ.get("ARIS4U_VALIDATION_LOG") or not log_file:
        return

    tool_input = inp.get("tool_input") or {}
    subagent_type = tool_input.get("subagent_type", "unknown")
    prompt = tool_input.get("prompt", "") or tool_input.get("description", "")
    model_param = tool_input.get("model")  # modelo pasado explícito; None = heredó (telemetría de routing)

    heads = {}
    for repo in [os.path.expanduser(p) for p in _lab_repos()]:
        if not os.path.isdir(os.path.join(repo, ".git")):
            continue
        try:
            out = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=3,
            )
            if out.returncode == 0:
                heads[repo] = out.stdout.strip()
        except Exception:
            continue

    ev = {
        "ts": datetime.now(UTC).isoformat(),
        "hook": "agent_dispatched",
        "event": "agent_dispatched",
        "subagent_type": subagent_type,
        "model_param": model_param,
        "prompt_preview": (prompt or "")[:200],
        "repo_heads_pre": heads,
    }
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(ev) + "\n")
    except Exception:
        return
