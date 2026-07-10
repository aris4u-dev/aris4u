"""Handlers PreToolUse advisory — guards de estándares (NO bloqueantes salvo gpu_crash).

Porta 1:1 los 7 guards de `hooks/guards/*.sh`. Cada uno es una función pura
`(tool_name, tool_input) -> Verdict` que devuelve ADVISE(msg) con el MISMO texto que el
`additionalContext` del .sh viejo, o PASS si no aplica / no hay violación.

Excepción: `gpu_crash` es BLOQUEANTE vía DENY (permissionDecision:"deny"), igual que
`gpu-crash-guard.sh` (que sale exit 0 con un JSON deny, no exit 2).

El orquestador acumula los ADVISE y los emite juntos; un DENY corta la cadena.
"""
from __future__ import annotations

import re
from typing import List

from dispatch.handlers import verdict as V

# ── type-hints-guard (Write|Edit, *.py) ──────────────────────────────────────

_DEF_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*\(")


def type_hints(tool_name: str, tool_input: dict) -> V.Verdict:
    file_path = (tool_input or {}).get("file_path") or "unknown.py"
    content = (tool_input or {}).get("content") or ""
    if not file_path.endswith(".py"):
        return V.ok()
    violations = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("@"):
            continue
        m = _DEF_RE.match(line)
        if not m:
            continue
        if m.group(1).startswith("__"):
            continue
        if "->" not in line:
            violations += 1
    if violations > 0:
        return V.advise(
            f"⚠️ TYPE HINTS: Missing return type on {violations} function(s). "
            "Add '-> ReturnType' to signatures."
        )
    return V.ok()


# ── docker-latest-guard (Write|Edit, Dockerfile/compose/*.yml) ───────────────

_DOCKER_RE = re.compile(r"(Dockerfile|docker-compose|\.ya?ml)$")


def docker_latest(tool_name: str, tool_input: dict) -> V.Verdict:
    file_path = (tool_input or {}).get("file_path") or "unknown.txt"
    content = (tool_input or {}).get("content") or ""
    if not _DOCKER_RE.search(file_path):
        return V.ok()
    violations = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^FROM[ \t]+", line):
            image = re.sub(r"^FROM[ \t]+([^ ]+).*", r"\1", line)
            if image.endswith(":latest") or ":" not in image:
                violations += 1
        if re.search(r"image:[ \t]", line):
            image = re.sub(r"^[ \t]*image:[ \t]*([^ ]+).*", r"\1", line)
            if image.endswith(":latest") or ":" not in image:
                violations += 1
    if violations > 0:
        return V.advise(
            f"⚠️ DOCKER VERSIONS: {violations} unversioned image(s). "
            "Pin versions (e.g., python:3.12.4, ubuntu:24.04)"
        )
    return V.ok()


# ── supabase-rls-guard (Write|Edit, *.sql / migrations) ──────────────────────

_CREATE_TABLE_RE = re.compile(
    r"^CREATE[ \t]+TABLE[ \t]+([A-Za-z_][A-Za-z0-9_]*)"
)
_RLS_RE = re.compile(r"ENABLE[ \t]+ROW[ \t]+LEVEL[ \t]+SECURITY")
_ALTER_TABLE_RE = re.compile(r".*ALTER[ \t]+TABLE[ \t]+([A-Za-z_][A-Za-z0-9_]*).*")


def supabase_rls(tool_name: str, tool_input: dict) -> V.Verdict:
    file_path = (tool_input or {}).get("file_path") or "unknown.sql"
    content = (tool_input or {}).get("content") or ""
    if not re.search(r"\.(sql|SQL)$", file_path) and "migrations" not in file_path:
        return V.ok()
    tables_created: List[str] = []
    rls_enabled: List[str] = []
    for line in content.splitlines():
        m = _CREATE_TABLE_RE.match(line)
        if m:
            tables_created.append(m.group(1))
        if _RLS_RE.search(line):
            am = _ALTER_TABLE_RE.match(line)
            # sed en el .sh: si la línea NO trae ALTER TABLE, deja la línea entera
            # (no matchea ningún nombre real) → cuenta como "no habilitado" para esa tabla.
            rls_enabled.append(am.group(1) if am else line)
    violations = 0
    for table in tables_created:
        if table not in rls_enabled:
            violations += 1
    if violations > 0:
        return V.advise(
            f"⚠️ SUPABASE RLS: {violations} table(s) missing ENABLE ROW LEVEL SECURITY. "
            "Add ALTER TABLE <name> ENABLE ROW LEVEL SECURITY"
        )
    return V.ok()


# ── spring-boot-pattern-guard (Write|Edit, *.java) ───────────────────────────


def spring_boot(tool_name: str, tool_input: dict) -> V.Verdict:
    file_path = (tool_input or {}).get("file_path") or ""
    content = (tool_input or {}).get("content") or ""
    if not file_path.endswith(".java"):
        return V.ok()
    if "@Autowired" in content:
        return V.advise(
            "⚠️ SPRING BOOT: 1 issue. Use constructor injection, not @Autowired."
        )
    return V.ok()


# ── screenshot-loop-guard (Bash, command) ────────────────────────────────────


def screenshot_loop(tool_name: str, tool_input: dict) -> V.Verdict:
    command = (tool_input or {}).get("command") or ""
    if "screenshot" not in command:
        return V.ok()
    count = command.count("screenshot")
    if count >= 2:
        return V.advise(
            f"⚠️ SCREENSHOT LOOP: {count} screenshot(s) in sequence. "
            "Use find()/javascript_tool for element verification instead."
        )
    return V.ok()


# ── kb-docs-validator-guard (Write|Edit, Claude/docs/*.md) ───────────────────

_KB_DATE_RE = re.compile(r"Actualizado.*[0-9]{4}-[0-9]{2}-[0-9]{2}")


def kb_docs(tool_name: str, tool_input: dict) -> V.Verdict:
    file_path = (tool_input or {}).get("file_path") or ""
    content = (tool_input or {}).get("content") or ""
    if not re.search(r"Claude/docs.*\.md$", file_path):
        return V.ok()
    violations = 0
    if any(tok in content for tok in ("TODO", "FIXME", "placeholder")):
        violations += 1
    if not _KB_DATE_RE.search(content):
        violations += 1
    if violations > 0:
        return V.advise(
            f"⚠️ KB DOCS: {violations} issue(s). No TODO/FIXME, "
            "add date header (Actualizado: YYYY-MM-DD)."
        )
    return V.ok()


# ── gpu-crash-guard (Bash|playwright navigate) — BLOQUEANTE vía DENY ──────────

_VIEWER_RE = re.compile(
    r"(localhost|127\.0\.0\.1):8901|scene3d/viewer|gaussian-splats|\.splat([^a-zA-Z]|$)",
    re.IGNORECASE,
)
_PLY_RE = re.compile(r"\.ply([^a-zA-Z]|$)", re.IGNORECASE)
_OPENER_RE = re.compile(
    r"(^|[;&| ])open( |$)|Google Chrome|Safari|firefox|chromium|browser_navigate|playwright",
    re.IGNORECASE,
)

_GPU_DENY_REASON = (
    "🛑 GPU-CRASH-GUARD: BLOQUEADO. El viewer scene3d (gaussian splats WebGL, :8901) "
    "puede tumbar el Mac (GPU 'progress timeout'). "
    "NO abrir el viewer/.ply/.splat en ningún browser de este Mac. "
    "Alternativas: (1) verificación estática (node --check, grep), "
    "(2) servir y renderizar en una máquina con GPU dedicada (CUDA). "
    "Para desbloquear: crea ~/.aris4u/gpu-crash-override"
)
_GPU_OVERRIDE_MSG = (
    "⚠️ GPU-CRASH-GUARD (override activo): este comando toca el viewer splat. "
    "Procede SOLO si las mitigaciones están aplicadas."
)


def gpu_crash(tool_name: str, tool_input: dict) -> V.Verdict:
    from pathlib import Path

    cmd = (tool_input or {}).get("command") or ""
    url = (tool_input or {}).get("url") or ""
    target = f"{cmd} {url}"
    if not target.strip():
        return V.ok()

    danger = False
    if url and (_VIEWER_RE.search(url) or _PLY_RE.search(url)):
        danger = True
    if _VIEWER_RE.search(target) and (
        _OPENER_RE.search(target) or "http.server" in target
        or "nohup" in target or "serve" in target
    ):
        danger = True
    if _PLY_RE.search(target) and _OPENER_RE.search(target):
        danger = True

    if not danger:
        return V.ok()

    override = Path.home() / ".aris4u" / "gpu-crash-override"
    if override.is_file():
        return V.advise(_GPU_OVERRIDE_MSG)

    return V.deny(_GPU_DENY_REASON)
