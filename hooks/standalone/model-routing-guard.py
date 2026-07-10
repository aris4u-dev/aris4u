#!/usr/bin/env python3
"""Guard bloqueante de gobierno de modelos (PreToolUse: Agent|Task|Workflow).

Materializa ~/.claude/rules/model-governance.md §2: ningún subagente hereda el
modelo del hilo (Fable/Opus) sin decisión explícita.

Bloquea (exit 2) cuando:
  - Agent/Task sin `model=` Y sin subagent_type cuyo frontmatter fije un modelo concreto.
  - Workflow con script (inline o scriptPath) que contiene agent(...) y CERO `model:`.
Fail-open (exit 0) ante cualquier error: el guard nunca rompe la sesión.

Nota: los agent() INTERNOS de un Workflow no pasan por PreToolUse (probado
2026-06-22, ver feedback_model_routing_workflows) — por eso este guard escanea
el SCRIPT en el momento de lanzar el Workflow, que sí pasa por hooks.

Portabilidad: usa Path.home() para ~/.claude/ estándar. El espejo al event log
de ARIS4U respeta el override canónico ARIS4U_EVENTS_LOG (env), con fallback a
~/projects/aris4u/logs/ y fail-open si no existe — no rompe en instalaciones
de terceros sin ese repo.
Fuente versionada: hooks/standalone/model-routing-guard.py (ARIS4U plugin).
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
_GUARD_LOG = HOME / ".claude" / "logs" / "guard-blocks.jsonl"
# Espejo al event log principal de ARIS4U para que amplification_score cuente el
# guard_block por-sesión. ARIS4U_EVENTS_LOG (override canónico, usado en tests)
# gana; si no, fallback al repo estándar. La escritura es fail-open + guardada
# por is_dir(), así que en un install sin ~/projects/aris4u simplemente no espeja.
_ARIS4U_EVENTS_LOG = Path(
    os.environ.get("ARIS4U_EVENTS_LOG")
    or (HOME / "projects" / "aris4u" / "logs" / "v16.1-events.jsonl")
)


def _append_guard_block(tool: str, reason: str, session_id: str = "") -> None:
    """Append a guard-block record and mirror it to the ARIS4U event log.

    The guard-blocks.jsonl entry carries session_id (from the Claude Code hook
    payload) so telemetry can attribute it. A second entry in the ARIS4U event
    log with event='model_routing_blocked' lets _read_session_signals_from_log
    count it in the guard_blocks signal of amplification_score. Both writes are
    fail-open: never raises.
    """
    ts = datetime.now(timezone.utc).isoformat()
    try:
        _GUARD_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps({
            "ts": ts,
            "guard": "model-routing",
            "tool": tool,
            "reason": reason,
            "session_id": session_id,
        }, ensure_ascii=False)
        with _GUARD_LOG.open("a", encoding="utf-8") as fh:
            fh.write(record + "\n")
    except Exception:
        pass
    # Mirror to the ARIS4U event log (in-repo, per-session), fail-open.
    try:
        if _ARIS4U_EVENTS_LOG.parent.is_dir():
            main_record = json.dumps({
                "ts": ts,
                "event": "model_routing_blocked",
                "guard": "model-routing",
                "tool": tool,
                "session_id": session_id,
            }, ensure_ascii=False)
            with _ARIS4U_EVENTS_LOG.open("a", encoding="utf-8") as fh:
                fh.write(main_record + "\n")
    except Exception:
        pass
GUIDE = (
    "síntesis/veredicto→opus · grueso (verify/search/review/edit/research)→sonnet · "
    "trivial (classify/format/count)→haiku · fable SOLO Fable-Gate"
)


def _frontmatter_model(agent_type: str) -> "str | None":
    """Modelo fijado en el frontmatter del agente, o None si es inherit/no existe."""
    name = agent_type.split(":", 1)[-1]
    candidates = [
        HOME / ".claude" / "agents" / f"{agent_type}.md",
        HOME / ".claude" / "agents" / f"{name}.md",
    ]
    for base in (HOME / ".claude" / "plugins" / "cache", HOME / ".claude" / "local-plugins"):
        try:
            if base.is_dir():
                candidates.extend(base.glob(f"**/agents/{name}.md"))
        except OSError:
            continue
    for p in candidates:
        try:
            if not p.is_file():
                continue
            head = p.read_text(encoding="utf-8", errors="ignore")[:4000]
            m = re.search(r"^model:\s*['\"]?([A-Za-z0-9.\-]+)", head, re.MULTILINE)
            if m and m.group(1).lower() not in ("inherit", "default"):
                return m.group(1)
        except OSError:
            continue
    return None


def _check_agent(ti: dict, session_id: str = "") -> int:
    """Agent/Task: exige model= o subagent_type con modelo fijo en frontmatter."""
    if ti.get("model"):
        return 0
    stype = str(ti.get("subagent_type") or "")
    if stype and _frontmatter_model(stype):
        return 0
    reason = (
        f"Agent(subagent_type='{stype or 'general-purpose'}') sin model= heredaría "
        f"el modelo del HILO (caro)"
    )
    print(
        f"⛔ GOBIERNO DE MODELOS (model-governance.md §2): "
        f"{reason}. Reintenta con model= explícito: {GUIDE}.",
        file=sys.stderr,
    )
    _append_guard_block("Agent", reason, session_id=session_id)
    return 2


def _check_workflow(ti: dict, session_id: str = "") -> int:
    """Workflow: si el script tiene agent() y cero 'model:', bloquea."""
    script = ti.get("script") or ""
    if not script and ti.get("scriptPath"):
        try:
            script = Path(str(ti["scriptPath"])).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return 0
    if not script:
        return 0  # workflow por nombre: script ya auditado en disco
    n_agents = len(re.findall(r"\bagent\s*\(", script))
    n_models = len(re.findall(r"\bmodel\s*:", script))
    # §2: TODO agent() lleva model:. Si hay MENOS 'model:' que agent(), al menos un
    # agent() hereda el modelo del HILO (peor caso: Fable, el más caro) y los agent()
    # internos NO pasan por PreToolUse — por eso se bloquea el SCRIPT aquí. El regex
    # tiende a SOBRE-contar 'model:' (meta.phases, opts.model), así que el fallo posible
    # es leniencia, nunca sobre-bloqueo de un script bien gobernado.
    if n_agents and n_models < n_agents:
        falta = "CERO" if n_models == 0 else f"solo {n_models}"
        reason = (
            f"script tiene {n_agents} agent() y {falta} 'model:' — "
            f"cada agent() sin model= hereda el HILO (peor caso Fable)"
        )
        print(
            f"⛔ GOBIERNO DE MODELOS (model-governance.md §2): el {reason}. "
            f"Añade model a CADA agent(): {GUIDE}.",
            file=sys.stderr,
        )
        _append_guard_block("Workflow", reason, session_id=session_id)
        return 2
    return 0


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool = data.get("tool_name", "")
    ti = data.get("tool_input") or {}
    if not isinstance(ti, dict):
        return 0
    session_id = str(data.get("session_id") or "")
    if tool in ("Agent", "Task"):
        return _check_agent(ti, session_id=session_id)
    if tool == "Workflow":
        return _check_workflow(ti, session_id=session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
