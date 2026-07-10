#!/usr/bin/env python3
"""Telemetría de ADOPCIÓN del enrutador de capacidades (Fase 4 — cierra GAP6).

El enrutador (``tools/capability_router.py``) ya SUGIERE capacidades vía el evento
``capability_hint``. Lo que faltaba era cerrar el lazo: medir si el modelo EFECTIVAMENTE
usa lo sugerido. Este módulo es el puente:

  1. ``register_hints``  — en UserPromptSubmit, tras inyectar el hint, guarda los nombres
     sugeridos en un estado PENDIENTE por-sesión (``/tmp/aris4u_hint_pending.json``, como
     el bridge de cliente). Antes de escribir, "cierra" el turno anterior (los que nunca
     se adoptaron → ``capability_ignored``).
  2. ``record_tool_use`` — en PostToolUse, cada vez que el modelo invoca una herramienta /
     skill / agente, se mapea esa invocación a su nombre de capacidad y, si casa con un
     hint PENDIENTE de esta sesión, se marca adoptado y se emite ``capability_adopted``.
  3. ``flush_ignored``   — en Stop (fin de cada turno), los hints pendientes sin adoptar
     del turno se cierran como ``capability_ignored``. Punto de cierre primario; el flush
     de ``register_hints`` es solo respaldo (idempotente).

Con los pares adopted/ignored, ``tools/conductor_stats.py`` calcula el hit-rate real.

LEYES (heredadas del enrutador):
  - GENÉRICO: el matcher es PURAMENTE estructural (parsea ``mcp__server__tool`` / Task /
    Skill / SlashCommand). CERO nombres de cliente. Funciona para el toolkit de cualquiera.
  - FAIL-OPEN: cada función traga sus errores; nada aquí puede romper un hook ni el flujo.
  - BARATO: un JSON pequeño en /tmp; sin red, sin DB, sin modelos. Apto para el hot path.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any
from collections.abc import Callable

ARIS_ROOT = Path(__file__).resolve().parent.parent

# Estado PENDIENTE por-sesión. Override por env para tests (como el bridge de cliente).
_DEFAULT_PENDING = Path("/tmp/aris4u_hint_pending.json")

# Cotas del estado (anti-crecimiento): edad máxima de una sesión y nº de sesiones.
_SESSION_MAX_AGE_SEC = 6 * 3600
_MAX_SESSIONS = 64

_SEP_RE = re.compile(r"[.:]|__")


# --------------------------------------------------------------------------- #
# Rutas y E/S fail-open
# --------------------------------------------------------------------------- #
def _pending_path() -> Path:
    """Ruta del estado pendiente (``ARIS4U_HINT_STATE`` la sobrescribe en tests)."""
    override = os.environ.get("ARIS4U_HINT_STATE")
    return Path(override) if override else _DEFAULT_PENDING


def _events_path() -> Path:
    """Ruta del event log (``ARIS4U_EVENTS_LOG`` la sobrescribe en tests)."""
    override = os.environ.get("ARIS4U_EVENTS_LOG")
    return Path(override) if override else ARIS_ROOT / "logs" / "v16.1-events.jsonl"


def _now_iso() -> str:
    """Timestamp ISO-8601 UTC."""
    return datetime.now(UTC).isoformat()


def _log_event(event: dict[str, Any]) -> None:
    """Append best-effort de un evento al event log. Nunca lanza."""
    try:
        lf = _events_path()
        if lf.parent.exists():
            with lf.open("a") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass


def _load_pending() -> dict[str, Any]:
    """Lee el estado pendiente. Fail-open a ``{"sessions": {}}``."""
    try:
        data = json.loads(_pending_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    except Exception:
        pass
    return {"sessions": {}}


def _save_pending(data: dict[str, Any]) -> None:
    """Persiste el estado pendiente. Nunca lanza."""
    try:
        _pending_path().write_text(json.dumps(data), encoding="utf-8")
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
            stale.append(sid)  # registro sin/with timestamp inválido → descartable
    for sid in stale:
        sessions.pop(sid, None)
    if len(sessions) > _MAX_SESSIONS:
        # Conserva las más recientes por 'updated' (orden lexicográfico ISO ≈ cronológico).
        ordered = sorted(sessions.items(), key=lambda kv: str(kv[1].get("updated", "")))
        for sid, _ in ordered[: len(sessions) - _MAX_SESSIONS]:
            sessions.pop(sid, None)


# --------------------------------------------------------------------------- #
# Matcher invocación → nombre(s) de capacidad (GENÉRICO, sin nombres de cliente)
# --------------------------------------------------------------------------- #
def _leaf(name: str) -> str:
    """Último segmento de un nombre tras los separadores ``.`` / ``:`` / ``__``."""
    parts = [p for p in _SEP_RE.split(name) if p]
    return parts[-1] if parts else name


def _first_str(ti: dict[str, Any], keys: tuple[str, ...]) -> str:
    """Primer valor str no vacío entre ``keys`` de ``ti`` (''-default)."""
    for k in keys:
        v = ti.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _ids_mcp(name: str) -> set[str]:
    """``mcp__<server>__<tool>`` → {``server.tool``, ``tool``, nombre completo}."""
    parts = name.split("__")
    if len(parts) < 3:
        return set()
    server, tool = parts[1], "__".join(parts[2:])
    return {f"{server}.{tool}", tool, name}


def _ids_for(tool_name: str, ti: dict[str, Any]) -> set[str]:
    """Identificadores crudos (sin normalizar) según el tipo de herramienta."""
    name = (tool_name or "").strip()
    nl = name.lower()
    if nl.startswith("mcp__"):
        return _ids_mcp(name)
    if nl in ("task", "agent"):
        st = _first_str(ti, ("subagent_type", "agent_type", "type"))
        return {st} if st else set()
    if nl == "skill":
        sk = _first_str(ti, ("skill", "name"))
        return {sk, _leaf(sk)} if sk else set()
    if nl == "slashcommand":
        cmd = _first_str(ti, ("command", "name"))
        first = cmd.split()[0].lstrip("/") if cmd else ""
        return {first, _leaf(first)} if first else set()
    return {name} if name else set()  # fallback genérico (rara vez casa; inofensivo)


def invocation_identifiers(tool_name: str, tool_input: dict[str, Any]) -> set[str]:
    """Identificadores de capacidad que una invocación de herramienta SATISFACE.

    Mapea la forma viva de invocación de Claude Code al/los nombre(s) con que el catálogo
    nombra esa capacidad. Es estructural: no conoce ningún cliente ni capacidad concreta.

      - MCP    ``mcp__<server>__<tool>``     → {``server.tool``, ``tool``, nombre completo}
      - Agente ``Task`` + ``subagent_type``  → {subagent_type}
      - Skill  ``Skill`` + ``skill``         → {skill, hoja(skill)}
      - Slash  ``SlashCommand`` + ``command``→ {primer-token sin '/', hoja}

    Args:
        tool_name: Nombre de la herramienta invocada (del payload PostToolUse).
        tool_input: Input de la herramienta (subagent_type / skill / command).

    Returns:
        Conjunto de identificadores en minúsculas (incluye hojas), o ``set()``.
    """
    ti = tool_input if isinstance(tool_input, dict) else {}
    raw = _ids_for(tool_name, ti)
    # Hojas + minúsculas para un match robusto frente a nombres cualificados.
    out = {i.lower() for i in raw if i}
    out |= {_leaf(i) for i in list(out)}
    return out


def _matches(pending_name: str, ids: set[str]) -> bool:
    """¿El hint ``pending_name`` lo satisface alguno de los identificadores ``ids``?"""
    pn = (pending_name or "").lower()
    if not pn:
        return False
    if pn in ids:
        return True
    return _leaf(pn) in ids


# --------------------------------------------------------------------------- #
# API del lazo: register / record / flush
# --------------------------------------------------------------------------- #
def peek_session(session_id: str) -> tuple[str, list[str]]:
    """Lee (sin mutar) la intención del turno y las capacidades ya adoptadas.

    Pensado para Stop: decidir el recordatorio de enforcement ANTES de cerrar el turno.
    Fail-open a ``("", [])``.

    Args:
        session_id: Sesión a inspeccionar.

    Returns:
        Tupla ``(intent, adopted_names)`` del turno en curso de esa sesión.
    """
    try:
        rec = _load_pending().get("sessions", {}).get(session_id or "")
        if not rec:
            return "", []
        adopted = [h.get("name", "") for h in rec.get("hints", []) if h.get("adopted")]
        return str(rec.get("intent", "")), [n for n in adopted if n]
    except Exception:
        return "", []


def flush_ignored(
    session_id: str, log_event: Callable[[dict[str, Any]], None] | None = None
) -> list[str]:
    """Cierra el turno: los hints pendientes SIN adoptar envejecen; se ignoran al 2do flush.

    Ventana de 2 turnos: en el primer flush (Stop del turno en que se sugirió) el hint
    recibe turn_age=1 y se mantiene vivo. En el segundo flush (Stop del turno siguiente)
    el hint se marca como capability_ignored. Esto cubre el patrón "hint en turno N,
    skill invocada en turno N+1" que antes contaba incorrectamente como ignorado.

    Idempotente dentro de un turno. Pensado para Stop (cierre primario). Fail-open.

    Args:
        session_id: Sesión a cerrar.
        log_event: Logger inyectable (tests); por defecto el del módulo.

    Returns:
        Nombres marcados como ignorados este turno (vacío si ninguno llegó a turn_age>=1).
    """
    log = log_event or _log_event
    sid = session_id or ""
    data = _load_pending()
    rec = data.get("sessions", {}).get(sid)
    if not rec:
        return []
    hints = rec.get("hints", [])
    ignored: list[str] = []
    surviving: list[dict] = []
    for h in hints:
        if h.get("adopted"):
            continue  # ya registrado como adoptado; no reemitir
        age = h.get("turn_age", 0)
        if age >= 1:
            # Segundo flush sin adopción → verdaderamente ignorado
            name = h.get("name", "")
            ignored.append(name)
            log(
                {
                    "ts": _now_iso(),
                    "event": "capability_ignored",
                    "name": name,
                    "intent": h.get("intent", ""),
                    "hinted_ts": h.get("ts", ""),
                    "session_id": sid,
                }
            )
        else:
            # Primer flush → dar una vuelta más de vida
            h["turn_age"] = 1
            surviving.append(h)
    if surviving:
        rec["hints"] = surviving
        rec["updated"] = _now_iso()
        _save_pending(data)
    else:
        data["sessions"].pop(sid, None)
        _save_pending(data)
    return ignored


def register_hints(
    session_id: str,
    hinted: list[str],
    intent: str = "",
    ts: str | None = None,
    log_event: Callable[[dict[str, Any]], None] | None = None,  # noqa: ARG001 — API compat
) -> None:
    """Guarda los hints recién inyectados como PENDIENTES de esta sesión.

    PRESERVA los hints supervivientes del turno anterior (turn_age=1) y añade los nuevos
    (turn_age=0). Los duplicados por nombre se descartan para no inflar el denominador.
    Poda sesiones viejas. Fail-open total.

    Args:
        session_id: Sesión actual.
        hinted: Nombres de capacidad sugeridos por el enrutador este turno.
        intent: Intención F1 del prompt (para hit-rate por intención).
        ts: Timestamp del hint (por defecto ahora).
        log_event: Logger inyectable (compat API; no usado desde que flush_ignored es
            el cierre primario en Stop).
    """
    try:
        sid = session_id or ""
        names = [str(h) for h in (hinted or []) if h]
        if not names:
            return
        data = _load_pending()
        sessions = data.setdefault("sessions", {})
        when = ts or _now_iso()
        existing = sessions.get(sid, {})
        # Preservar hints supervivientes (turn_age=1) para darles su segunda oportunidad.
        surviving = [h for h in existing.get("hints", []) if not h.get("adopted")]
        existing_names = {h.get("name", "") for h in surviving}
        new_hints = [
            {"name": n, "intent": intent, "ts": when, "adopted": False, "turn_age": 0}
            for n in names if n not in existing_names
        ]
        sessions[sid] = {
            "updated": when,
            "intent": intent,
            "hints": surviving + new_hints,
        }
        _prune(sessions)
        _save_pending(data)
    except Exception:
        pass


def record_tool_use(
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    log_event: Callable[[dict[str, Any]], None] | None = None,
) -> list[str]:
    """Marca como ADOPTADO todo hint pendiente que esta invocación satisface.

    Para cada hint pendiente no-adoptado de la sesión cuyo nombre case con la invocación,
    lo marca adoptado y emite ``capability_adopted``. Solo afecta a capacidades que fueron
    SUGERIDAS (la adopción solo tiene sentido relativa a un hint). Fail-open total.

    Args:
        session_id: Sesión actual.
        tool_name: Herramienta invocada (payload PostToolUse).
        tool_input: Input de la herramienta.
        log_event: Logger inyectable (tests).

    Returns:
        Nombres de capacidad adoptados por esta invocación (vacío si ninguno).
    """
    log = log_event or _log_event
    try:
        sid = session_id or ""
        data = _load_pending()
        rec = data.get("sessions", {}).get(sid)
        if not rec:
            return []
        ids = invocation_identifiers(tool_name, tool_input)
        if not ids:
            return []
        adopted: list[str] = []
        for h in rec.get("hints", []):
            if h.get("adopted"):
                continue
            if _matches(h.get("name", ""), ids):
                h["adopted"] = True
                name = h.get("name", "")
                adopted.append(name)
                log(
                    {
                        "ts": _now_iso(),
                        "event": "capability_adopted",
                        "name": name,
                        "intent": h.get("intent", ""),
                        "hinted_ts": h.get("ts", ""),
                        "tool": tool_name,
                        "session_id": sid,
                    }
                )
        if adopted:
            rec["updated"] = _now_iso()
            _save_pending(data)
        return adopted
    except Exception:
        return []
