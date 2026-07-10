"""Handler schema_drift — portado de hooks/schema_drift.sh (PostToolUse Write|Edit|MultiEdit).

Monitorea cambios de código en proyectos-lab para drift de esquema (multi-stack:
Supabase/Flutter/Flyway/Prisma). Corre tools/schema_compat_check.py, emite un evento
JSONL de telemetría y avisa por stderr SOLO si hay errores reales (no bloquea).

Side-effect: línea JSONL en ARIS4U_LOG_FILE (si el validation log está activo) +
stderr opcional. No emite stdout. `run()` devuelve un warning-string para que el
orquestador lo escriba a stderr (equivalente al heredoc `cat >&2` del .sh).
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone, UTC
from pathlib import Path

from dispatch.contract import ARIS4U_ROOT

# Vacío por defecto — config ausente ⇒ sin proyectos-lab monitoreados.
# Para activar, configura "lab_projects" en ~/.aris4u/config.json:
#   {"lab_projects": [{"path": "/home/user/projects/mi-proyecto"}]}
_DEFAULT_LAB_PROJECT_NAMES: list[str] = []


def _load_lab_projects() -> list[str]:
    """Carga la lista de rutas de proyectos-lab desde config o devuelve el default exacto.

    Config: ~/.aris4u/config.json campo "lab_projects" (lista de dicts con "path" str).
    Si el campo no existe o falla, devuelve los paths default (idénticos al comportamiento
    previo, byte por byte).

    Returns:
        Lista de rutas con '/' final (misma semántica que el default hardcodeado).
    """
    try:
        import json as _json
        import os as _os

        cfg_path = _os.environ.get("ARIS4U_CONFIG") or str(Path.home() / ".aris4u" / "config.json")
        p = Path(cfg_path)
        if p.is_file():
            cfg = _json.loads(p.read_text())
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
                        paths.append(raw + "/")
                if paths:
                    return paths
    except Exception:
        pass
    return [str(Path.home() / "projects" / name) + "/" for name in _DEFAULT_LAB_PROJECT_NAMES]


_LAB_PROJECTS: list[str] = _load_lab_projects()

_RELEVANT_SUFFIXES = (
    "/supabase/migrations/",
    "/lib/services/",
    "/src/main/resources/db/migration/",
    "/prisma/migrations/",
)


def _is_relevant(file_path: str) -> bool:
    """Replica el case shell multi-stack de archivos schema-relevantes."""
    for marker in _RELEVANT_SUFFIXES:
        if marker in file_path:
            return True
    if "/src/main/java/" in file_path and file_path.endswith(".java"):
        return True
    if "/src/main/kotlin/" in file_path and file_path.endswith(".kt"):
        return True
    if file_path.endswith("/prisma/schema.prisma"):
        return True
    return False


def _find_repo_root(file_path: str, fallback: str) -> str:
    """Sube hasta encontrar marcador de proyecto (multi-stack); fallback al lab dir."""
    current = os.path.dirname(file_path)
    markers = (
        ".git",
        "pubspec.yaml",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "package.json",
        "pyproject.toml",
    )
    while current and current != "/":
        for m in markers:
            p = os.path.join(current, m)
            if (m == ".git" and os.path.isdir(p)) or (m != ".git" and os.path.isfile(p)):
                return current
        current = os.path.dirname(current)
    return fallback


def _resolve_repo_root(tool_name: str, tool_input: dict | None) -> tuple[str, str]:
    """Aplica el gating (tool, lab project, relevancia) y resuelve la raíz del repo.

    Args:
        tool_name: nombre del tool (solo Write/Edit/MultiEdit pasan).
        tool_input: tool_input del evento (usa file_path).

    Returns:
        Tupla (file_path, repo_root). Ambos "" cuando el evento no aplica
        (tool no soportado, sin file_path, fuera de lab, no schema-relevante,
        o sin raíz de repo).
    """
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return "", ""
    file_path = (tool_input or {}).get("file_path") or ""
    if not file_path:
        return "", ""

    lab_project = ""
    for lab in _LAB_PROJECTS:
        if file_path.startswith(lab):
            lab_project = lab
            break
    if not lab_project:
        return "", ""

    if not _is_relevant(file_path):
        return "", ""

    repo_root = _find_repo_root(file_path, lab_project)
    if not repo_root:
        return "", ""
    return file_path, repo_root


def _parse_drift_meta(check_output: str) -> tuple[str, int, int]:
    """Parsea el footer estructurado {"_meta": true, ...} de la salida del check.

    Recorre todas las líneas y conserva la última que matchee (comportamiento
    histórico del .sh portado).

    Args:
        check_output: stdout+stderr combinados del schema_compat_check.

    Returns:
        Tupla (source, drift_errors, drift_warnings). source es "unknown" si no
        hay footer parseable.
    """
    source = "unknown"
    drift_errors = 0
    drift_warnings = 0
    for line in check_output.splitlines():
        if line.startswith('{"_meta": true'):
            try:
                meta = json.loads(line)
                source = meta.get("source", "unknown") or "unknown"
                drift_errors = int(meta.get("errors", 0) or 0)
                drift_warnings = int(meta.get("warnings", 0) or 0)
            except Exception:
                pass
    return source, drift_errors, drift_warnings


def _event_name_for(source: str) -> str:
    """Mapea el source del check al nombre de evento de telemetría.

    Args:
        source: 'db', 'static' o 'unknown'.

    Returns:
        'schema_check_db' / 'schema_check_static' / 'schema_check_skipped'.
    """
    if source == "db":
        return "schema_check_db"
    if source == "static":
        return "schema_check_static"
    return "schema_check_skipped"


def _detect_stack(repo_root: str) -> str:
    """Enriquece con el stack vía detect_stack_cli.py si está disponible.

    Args:
        repo_root: raíz del repo a inspeccionar.

    Returns:
        El stack detectado, o "generic" si el cli no existe, falla o no imprime.
    """
    detect_cli = str(ARIS4U_ROOT / "tools" / "detect_stack_cli.py")
    if not os.path.isfile(detect_cli):
        return "generic"
    try:
        return (
            subprocess.run(
                ["python3", detect_cli, repo_root],
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
            or "generic"
        )
    except Exception:
        return "generic"


def _write_telemetry(event: dict) -> None:
    """Anexa una línea JSONL del evento al validation log si está activo (fail-open).

    Args:
        event: el dict del evento a serializar.
    """
    log_file = os.environ.get("ARIS4U_LOG_FILE")
    if os.environ.get("ARIS4U_VALIDATION_LOG") and log_file:
        try:
            with open(log_file, "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception:
            pass


def run(tool_name: str, tool_input: dict | None) -> str:
    """Corre el chequeo de drift de esquema. Devuelve warning para stderr ("" si none).

    Args:
        tool_name: nombre del tool (solo Write/Edit/MultiEdit).
        tool_input: tool_input del evento (usa file_path).

    Returns:
        Texto de advertencia para stderr, o "" si no aplica / sin errores.
    """
    file_path, repo_root = _resolve_repo_root(tool_name, tool_input)
    if not repo_root:
        return ""

    schema_check = str(ARIS4U_ROOT / "tools" / "schema_compat_check.py")
    if not os.path.isfile(schema_check):
        return ""

    ts = datetime.now(UTC).isoformat()

    proc = subprocess.run(
        ["python3", schema_check, repo_root],
        capture_output=True,
        text=True,
    )
    check_output = (proc.stdout or "") + (proc.stderr or "")
    schema_exit = proc.returncode

    source, drift_errors, drift_warnings = _parse_drift_meta(check_output)
    drift_count = drift_errors + drift_warnings
    event_name = _event_name_for(source)
    stack = _detect_stack(repo_root)

    _write_telemetry(
        {
            "ts": ts,
            "hook": "schema_drift",
            "event": event_name,
            "stack": stack,
            "repo": repo_root[:200],
            "exit": schema_exit,
            "source": source,
            "drift_errors": drift_errors,
            "drift_warnings": drift_warnings,
            "drift_count": drift_count,
            "trigger_file": file_path[:300],
        }
    )

    # Warn solo cuando el check corrió Y hay errores reales.
    if source != "unknown" and drift_errors > 0:
        return (
            f"⚠️  Schema drift detected in {repo_root} [{source} mode]\n\n"
            f"  Changes to: {file_path}\n"
            f"  Errors:   {drift_errors}\n"
            f"  Warnings: {drift_warnings}\n\n"
            "  Run to investigate:\n"
            f"    python3 {schema_check} {repo_root}"
        )
    return ""
