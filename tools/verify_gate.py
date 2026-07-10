#!/usr/bin/env python3
"""Rastreador de SEÑALES DE VERIFICACIÓN por turno — el lazo VERIFICAR-AL-CERRAR.

VERIFICACIÓN DE CÓDIGO = pilar nº1 del producto (junto a memoria). Para un developer que
instala ARIS4U, el sistema debe VERIFICAR su código y no dejar pasar código sin revisar.
Ya existen los gates que actúan DURANTE el trabajo: ``code_quality_gate`` (ruff por-edit),
``commit_quality_gate`` (pyright + tests afectados por-commit) y ``migration_linter``. Lo
que faltaba era cerrar el lazo AL CERRAR el turno: ¿se tocó código y nadie lo verificó?

Este módulo recolecta, por sesión y de forma BARATA, dos señales runtime a partir de cada
PostToolUse, para que el Stop-hook pueda emitir un recordatorio SUAVE (nunca un bloqueo):

  - ``code_touched``     — hubo un Write/Edit/MultiEdit sobre un archivo de CÓDIGO
                           (.py/.ts/.tsx/.js/.dart/.go/.rs/.java/.kt/… — genérico, cualquier stack).
  - ``verify_ran``       — se corrió una verificación: un comando de tests/lint/types
                           (pytest/ruff/pyright/mypy/eslint/tsc/jest/flutter analyze/go test/…)
                           o se invocó una capacidad de cierre del inventario
                           (second-auditor / code-review / verify-claims / aris_dialectic).

LEYES (heredadas del enrutador de capacidades):
  - GENÉRICO: la detección es PURAMENTE estructural (extensiones de archivo + patrones de
    comando de herramientas estándar). CERO nombres de cliente. Sirve para el toolkit de
    cualquiera.
  - FAIL-OPEN: cada función traga sus errores; nada aquí puede romper un hook ni el flujo de
    sesión. Ante cualquier duda, devuelve la señal "segura" (no molestar).
  - BARATO: un JSON pequeño en /tmp; sin red, sin DB, sin modelos. Apto para el hot path de
    PostToolUse, que corre en CADA herramienta.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

# Estado por-sesión. Override por env para tests (como el bridge de cliente / adopción).
_DEFAULT_STATE = Path("/tmp/aris4u_verify_signal.json")

# Cotas del estado (anti-crecimiento): edad máxima de una sesión y nº de sesiones.
_SESSION_MAX_AGE_SEC = 6 * 3600
_MAX_SESSIONS = 64

# Extensiones que cuentan como CÓDIGO (genérico, multi-stack). Tocar uno de estos en un
# turno de implementation/fix es lo que dispara la expectativa de verificación.
_CODE_EXTS = frozenset(
    {
        ".py", ".pyi",
        ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
        ".dart",
        ".go",
        ".rs",
        ".java", ".kt", ".kts", ".scala",
        ".rb",
        ".php",
        ".c", ".h", ".cc", ".cpp", ".hpp", ".cxx",
        ".cs",
        ".swift",
        ".sql",
        ".sh", ".bash",
        ".vue", ".svelte", ".astro",
    }
)

_EDIT_TOOLS = frozenset({"Write", "Edit", "MultiEdit"})

# Patrones de comando que cuentan como "se corrió una verificación" (tests/lint/types).
# Genéricos: las herramientas estándar de cada ecosistema. Se buscan como token de comando.
_VERIFY_CMD_RE = re.compile(
    r"\b("
    r"pytest|py\.test|unittest|nox|tox|"
    r"ruff|flake8|pylint|mypy|pyright|black\s+--check|isort\s+--check|"
    r"eslint|tsc|jest|vitest|mocha|"
    r"flutter\s+test|flutter\s+analyze|dart\s+analyze|dart\s+test|"
    r"go\s+test|go\s+vet|golangci-lint|"
    r"cargo\s+test|cargo\s+clippy|cargo\s+check|"
    r"npm\s+(?:run\s+)?test|npm\s+run\s+lint|yarn\s+test|pnpm\s+test|"
    r"gradle\s+test|mvn\s+test|\./gradlew\s+test|"
    r"phpunit|rspec|rubocop|"
    r"check"  # supabase db reset linter / genérico "make check"
    r")\b"
)

# Hojas de capacidades de VERIFICACIÓN del inventario (gate de cierre opt-in). Espejo de
# orchestration_protocol VERIFICAR / conductor_enforce. Match por hoja (último segmento).
_VERIFY_LEAVES = frozenset(
    {"second-auditor", "code-review", "verify-claims", "aris_dialectic", "review"}
)


# --------------------------------------------------------------------------- #
# Rutas y E/S fail-open
# --------------------------------------------------------------------------- #
def _state_path() -> Path:
    """Ruta del estado de señales (``ARIS4U_VERIFY_STATE`` la sobrescribe en tests)."""
    override = os.environ.get("ARIS4U_VERIFY_STATE")
    return Path(override) if override else _DEFAULT_STATE


def _now_iso() -> str:
    """Timestamp ISO-8601 UTC."""
    return datetime.now(UTC).isoformat()


def _load() -> dict[str, Any]:
    """Lee el estado de señales. Fail-open a ``{"sessions": {}}``."""
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    except Exception:
        pass
    return {"sessions": {}}


def _save(data: dict[str, Any]) -> None:
    """Persiste el estado de señales. Nunca lanza."""
    try:
        _state_path().write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _prune(sessions: dict[str, Any]) -> None:
    """Poda sesiones viejas y limita el nº total (in-place). Tolerante a basura."""
    now = datetime.now(UTC)
    stale: list[str] = []
    for sid, rec in list(sessions.items()):
        try:
            updated = datetime.fromisoformat(str(rec.get("updated", "")))
            if (now - updated).total_seconds() > _SESSION_MAX_AGE_SEC:
                stale.append(sid)
        except Exception:
            stale.append(sid)
    for sid in stale:
        sessions.pop(sid, None)
    if len(sessions) > _MAX_SESSIONS:
        ordered = sorted(sessions.items(), key=lambda kv: str(kv[1].get("updated", "")))
        for sid, _ in ordered[: len(sessions) - _MAX_SESSIONS]:
            sessions.pop(sid, None)


# --------------------------------------------------------------------------- #
# Detección estructural (genérica, sin nombres de cliente)
# --------------------------------------------------------------------------- #
def _leaf(name: str) -> str:
    """Último segmento de un nombre tras ``.`` / ``:`` / ``__`` (minúsculas)."""
    out = (name or "").lower()
    for sep in (".", ":", "__"):
        out = out.split(sep)[-1]
    return out


def _is_code_edit(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """¿La invocación es una escritura sobre un archivo de código?"""
    if tool_name not in _EDIT_TOOLS:
        return False
    fp = tool_input.get("file_path")
    if not isinstance(fp, str) or not fp:
        return False
    return Path(fp).suffix.lower() in _CODE_EXTS


def _first_str(ti: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Primer valor str no vacío entre ``keys`` de ``ti`` (''-default)."""
    for k in keys:
        v = ti.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


# Mapa tool→claves de input de las que extraer el nombre de capacidad invocada.
_CAP_ID_KEYS: dict[str, tuple[str, ...]] = {
    "skill": ("skill", "name"),
    "task": ("subagent_type", "agent_type", "type"),
    "agent": ("subagent_type", "agent_type", "type"),
    "slashcommand": ("command", "name"),
}


def _verify_capability_id(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Identificador de la capacidad de cierre invocada (Skill/Task/SlashCommand/MCP), o ''."""
    nl = (tool_name or "").strip().lower()
    if nl.startswith("mcp__"):
        return tool_name or ""
    raw = _first_str(tool_input, _CAP_ID_KEYS.get(nl, ()))
    if nl == "slashcommand":
        return raw.split()[0].lstrip("/") if raw else ""
    return raw


def _is_verification(tool_name: str, tool_input: dict[str, Any]) -> bool:
    """¿La invocación es una verificación (comando de tests/lint o capacidad de cierre)?"""
    if (tool_name or "").strip().lower() == "bash":
        cmd = tool_input.get("command")
        return bool(isinstance(cmd, str) and cmd and _VERIFY_CMD_RE.search(cmd))
    # Capacidad de cierre invocada (Skill/Task/SlashCommand/MCP) → match por hoja.
    candidate = _verify_capability_id(tool_name, tool_input)
    return bool(candidate) and _leaf(candidate) in _VERIFY_LEAVES


# --------------------------------------------------------------------------- #
# API: record / query / reset
# --------------------------------------------------------------------------- #
def record_tool(session_id: str, tool_name: str, tool_input: dict[str, Any]) -> None:
    """Actualiza las señales del turno a partir de una invocación de herramienta.

    Pensado para PostToolUse (corre en CADA herramienta). Marca ``code_touched`` si fue una
    escritura de código, y ``verify_ran`` si fue una verificación (comando de tests/lint o
    capacidad de cierre del inventario). Idempotente (las señales solo se ELEVAN a True).
    Fail-open total: nunca lanza.

    Args:
        session_id: Sesión actual (de ``ARIS4U_SESSION_ID``).
        tool_name: Nombre de la herramienta invocada (payload PostToolUse).
        tool_input: Input de la herramienta (file_path / command / skill / subagent_type).
    """
    try:
        ti = tool_input if isinstance(tool_input, dict) else {}
        code = _is_code_edit(tool_name, ti)
        verify = _is_verification(tool_name, ti)
        if not code and not verify:
            return  # invocación irrelevante a la verificación → no tocar el estado
        sid = session_id or ""
        data = _load()
        sessions = data.setdefault("sessions", {})
        rec = sessions.setdefault(sid, {"code_touched": False, "verify_ran": False})
        if code:
            rec["code_touched"] = True
        if verify:
            rec["verify_ran"] = True
        rec["updated"] = _now_iso()
        _prune(sessions)
        _save(data)
    except Exception:
        pass


def _flag(session_id: str, key: str) -> bool:
    """Lee una señal booleana de la sesión. Fail-open a False."""
    try:
        rec = _load().get("sessions", {}).get(session_id or "")
        return bool(rec.get(key)) if isinstance(rec, dict) else False
    except Exception:
        return False


def code_was_touched(session_id: str) -> bool:
    """¿Se editó algún archivo de código en este turno? Fail-open a False."""
    return _flag(session_id, "code_touched")


def verification_ran(session_id: str) -> bool:
    """¿Se corrió alguna verificación (tests/lint/types o gate de cierre)? Fail-open a False."""
    return _flag(session_id, "verify_ran")


def reset_session(session_id: str) -> None:
    """Limpia las señales de una sesión (llamar en Stop, tras evaluar el recordatorio).

    El Stop cierra el turno: las señales son por-turno, así que se resetean para que el
    siguiente turno empiece limpio. Fail-open total.

    Args:
        session_id: Sesión a resetear.
    """
    try:
        sid = session_id or ""
        data = _load()
        if data.get("sessions", {}).pop(sid, None) is not None:
            _save(data)
    except Exception:
        pass
