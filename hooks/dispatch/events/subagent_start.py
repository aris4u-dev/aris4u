"""Handler SubagentStart — portado de hooks/subagent_depth.sh (sin heredoc shell).

Resuelve Locked Decision #12: el depth protocol propaga a los subagentes. Inyecta
en el contexto del subagente: locked decisions, critical guards, quality requirements,
arquitectura del proyecto (.planning/) y el plan de waves del orquestador.

Equivalencia verificada contra el .sh viejo vía tests/dispatch/golden/subagent_start_*.
"""
from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone, UTC
from pathlib import Path

from dispatch.contract import ARIS4U_ROOT, emit_additional_context

STATE_FILE = Path("/tmp/aris4u_session_state.json")
SESSIONS_DB = ARIS4U_ROOT / "data" / "sessions.db"


def _load_state() -> dict:
    """Lee el estado de sesión desde STATE_FILE (fail-open).

    Returns:
        El dict de estado, o ``{}`` si el archivo no existe o el JSON es inválido.
    """
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _locked_lines(state: dict, get_locked_decisions: Callable | None) -> list[str]:
    """Construye las líneas del bloque LOCKED DECISIONS (fail-open).

    Solo produce salida si la DB de sesiones existe, hay un ``last_query`` en el
    estado y la consulta devuelve decisiones.

    Args:
        state: El estado de sesión (usa ``last_query``).
        get_locked_decisions: Resolver de decisiones, o ``None`` si el engine no
            está disponible.

    Returns:
        Las líneas (header + bullets) del bloque, o ``[]`` si no aplica.
    """
    if not (SESSIONS_DB.exists() and get_locked_decisions):
        return []
    query = state.get("last_query", "")
    if not query:
        return []
    try:
        locked = get_locked_decisions(query, limit=5)
    except Exception:
        locked = []
    if not locked:
        return []
    lines = ["LOCKED DECISIONS (do NOT contradict):"]
    for d in locked:
        ref = d.get("session_ref", "")
        lines.append(f'- [{ref}] {d["decision"]}')
    return lines


def _guard_lines(get_all_guards: Callable | None) -> list[str]:
    """Construye las líneas del bloque CRITICAL GUARDS (dedup + cap 6, fail-open).

    Solo produce salida si la DB de sesiones existe y hay guards de severidad
    ``critical``. Deduplica por patrón y limita a 6 patrones.

    Args:
        get_all_guards: Resolver de guards, o ``None`` si el engine no está
            disponible.

    Returns:
        Las líneas (header + bullets) del bloque, o ``[]`` si no aplica.
    """
    if not (SESSIONS_DB.exists() and get_all_guards):
        return []
    try:
        guards = get_all_guards()
    except Exception:
        guards = []
    critical = [g for g in guards if g.get("severity") == "critical"]
    if not critical:
        return []
    lines = ["CRITICAL GUARDS:"]
    seen: set = set()
    for g in critical:
        p = g["pattern"]
        if p in seen:
            continue
        seen.add(p)
        lines.append(f"- {p}")
        if len(seen) >= 6:
            break
    return lines


def _quality_lines() -> list[str]:
    """Construye las líneas estáticas del bloque QUALITY REQUIREMENTS.

    Returns:
        El header y los 4 bullets de requisitos de calidad.
    """
    return [
        "QUALITY REQUIREMENTS:",
        "- Write COMPLETE code, not skeletons or TODOs",
        "- Include input validation and error handling",
        "- Verify your work compiles/runs before returning",
        "- If implementation: describe user-testable verification steps",
    ]


def _architecture_lines() -> list[str]:
    """Construye las líneas de PROJECT ARCHITECTURE desde .planning/ (fail-open).

    Returns:
        La línea con el header y los primeros 800 chars de ARCHITECTURE.md, o
        ``[]`` si el archivo no existe o no se puede leer.
    """
    arch_file = Path.cwd() / ".planning" / "ARCHITECTURE.md"
    if not arch_file.exists():
        return []
    try:
        arch_content = arch_file.read_text()[:800]
    except Exception:
        return []
    return [f"PROJECT ARCHITECTURE (from .planning/):\n{arch_content}"]


def _wave_lines() -> list[str]:
    """Construye las líneas del bloque AGENT EXECUTION PLAN del orquestador (fail-open).

    Returns:
        El header, una línea por wave y (si hay progreso) la línea de progreso,
        o ``[]`` si no hay waves o el orquestador falla.
    """
    try:
        from engine.v16.agent_orchestrator import AgentOrchestrator

        orch = AgentOrchestrator()
        waves = orch.get_waves()
        if not waves:
            return []
        lines = ["AGENT EXECUTION PLAN:"]
        for i, wave in enumerate(waves):
            lines.append(f'  Wave {i + 1}: {", ".join(wave)}')
        summary = orch.summary()
        if summary.get("completed", 0) > 0:
            lines.append(f'  Progress: {summary["completed"]}/{summary["total"]} completed')
        return lines
    except Exception:
        return []


def _track_launch(state: dict) -> None:
    """Registra el lanzamiento del subagente en el estado de sesión (fail-open).

    Incrementa ``research_agents_launched`` y añade ``Agent`` a ``tools_used`` sin
    duplicar, persistiendo el estado a STATE_FILE.

    Args:
        state: El estado de sesión a mutar y persistir.
    """
    try:
        state["research_agents_launched"] = state.get("research_agents_launched", 0) + 1
        state.setdefault("tools_used", [])
        if "Agent" not in state["tools_used"]:
            state["tools_used"].append("Agent")
        STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass


def _emit_telemetry(inp: dict, parts: list[str], state: dict) -> None:
    """Anexa una línea de telemetría V16.1 al log de validación (fail-open).

    Idéntica al hook viejo: solo escribe si ``ARIS4U_VALIDATION_LOG`` y
    ``ARIS4U_LOG_FILE`` están en el entorno.

    Args:
        inp: El payload del evento (de él se extraen subagent_type y prompt).
        parts: Las líneas ya construidas (se cuentan locked/guards inyectados).
        state: El estado de sesión (para subagents_this_session).
    """
    if not (os.environ.get("ARIS4U_VALIDATION_LOG") and os.environ.get("ARIS4U_LOG_FILE")):
        return
    subagent_type = inp.get("subagent_type") or inp.get("tool_input", {}).get("subagent_type", "unknown")
    prompt_preview = (inp.get("prompt") or inp.get("tool_input", {}).get("prompt", ""))[:200]
    try:
        with open(os.environ["ARIS4U_LOG_FILE"], "a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "hook": "subagent_depth",
                        "event": "subagent_start",
                        "subagent_type": subagent_type,
                        "prompt_preview": prompt_preview,
                        "locked_decisions_injected": len([p for p in parts if p.startswith("- [")]),
                        "guards_injected": (6 if any("CRITICAL GUARDS" in p for p in parts) else 0),
                        "subagents_this_session": state.get("research_agents_launched", 0),
                    }
                )
                + "\n"
            )
    except Exception:
        pass


def handle(event_name: str, inp: dict) -> None:
    """Inyecta el contexto de depth propagation en el subagente que arranca.

    Orquesta los 6 pasos del hook: locked decisions, critical guards, quality
    requirements, arquitectura del proyecto, plan de waves y state tracking,
    luego emite todo como additionalContext y registra telemetría. Cada paso es
    fail-open: un fallo en uno no impide los demás ni la emisión final.

    Args:
        event_name: Nombre del evento (``"SubagentStart"``); no usado, parte del
            contrato del dispatcher.
        inp: El payload del evento (subagent_type, prompt, etc.).
    """
    if str(ARIS4U_ROOT) not in sys.path:
        sys.path.insert(0, str(ARIS4U_ROOT))

    parts = ["[ARIS4U DEPTH PROPAGATION — injected by SubagentStart hook]"]

    state = _load_state()

    try:
        from engine.v16.session_manager import get_all_guards, get_locked_decisions
    except Exception:
        get_all_guards = None
        get_locked_decisions = None

    # 1. Locked decisions (si hay query previa en el estado de sesión)
    parts.extend(_locked_lines(state, get_locked_decisions))

    # 2. Critical guards (dedup, máx 6)
    parts.extend(_guard_lines(get_all_guards))

    # 3. Quality enforcement
    parts.extend(_quality_lines())

    # 4. Domain context desde .planning/ARCHITECTURE.md
    parts.extend(_architecture_lines())

    # 5. Plan de waves del orquestador
    parts.extend(_wave_lines())

    # 6. Track del lanzamiento en el estado de sesión
    _track_launch(state)

    # Telemetría V16.1 (idéntica al hook viejo)
    _emit_telemetry(inp, parts, state)

    emit_additional_context("\n".join(parts))
