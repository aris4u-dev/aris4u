#!/usr/bin/env python3
"""Medidor del enrutador de capacidades — uplift de los hints (paso 4: MEDIR).

Lee el event log y correlaciona cada ``capability_hint`` (lo que el enrutador sugirió)
con el USO real posterior de esa capacidad en la misma línea de tiempo:
  - tools MCP  → evento ``mcp_tool`` (campo ``tool``); el hint "aris4u.aris_recall_client"
    casa con tool="aris_recall_client".
  - agentes    → evento ``subagent_start`` (campo ``subagent_type``).
  - skills/commands → sin telemetría de invocación directa ⇒ NO medibles (se reportan aparte).

Responde la pregunta de "medir antes de ampliar": ¿inyectar el hint sube el uso real?
Con pocos datos el reporte lo dice honestamente (data_sufficiency=low).

Uso:
    python3 tools/capability_hint_report.py
    python3 tools/capability_hint_report.py --json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ARIS_ROOT = Path(__file__).resolve().parent.parent
EVENTS_LOG = ARIS_ROOT / "logs" / "v16.1-events.jsonl"


def load_events(path: Path | None = None) -> list[dict[str, Any]]:
    """Carga el event log JSONL (líneas inválidas se ignoran)."""
    p = path or EVENTS_LOG
    out: list[dict[str, Any]] = []
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _usage_index(events: list[dict[str, Any]]) -> tuple[dict[str, list[str]], dict[str, list[str]], set[str]]:
    """Construye timelines de uso: tools MCP y agentes (nombre → lista de ts ordenada)."""
    mcp: dict[str, list[str]] = {}
    agents: dict[str, list[str]] = {}
    for e in events:
        ev = e.get("event")
        if ev == "mcp_tool" and e.get("tool"):
            mcp.setdefault(e["tool"], []).append(e.get("ts", ""))
        elif ev == "subagent_start" and e.get("subagent_type"):
            agents.setdefault(e["subagent_type"], []).append(e.get("ts", ""))
    for d in (mcp, agents):
        for k in d:
            d[k].sort()
    return mcp, agents, set(agents)


def _resolve(name: str, agent_types: set[str]) -> tuple[str, str]:
    """Mapea el nombre del hint a (clave_uso, tipo): mcp / agent / unmeasurable."""
    if "." in name:  # "aris4u.aris_recall_client" → tool MCP
        return name.split(".", 1)[1], "mcp"
    if name in agent_types:
        return name, "agent"
    return name, "unmeasurable"  # skill/command: sin telemetría de invocación


def _used_after(ts_list: list[str], hint_ts: str) -> bool:
    """¿Hay un uso con timestamp POSTERIOR al hint?"""
    if not hint_ts:
        return False
    return any(t and t > hint_ts for t in ts_list)


def _accumulate(
    by_cap: dict[str, dict[str, Any]],
    name: str,
    hint_ts: str,
    mcp: dict[str, list[str]],
    agents: dict[str, list[str]],
    agent_types: set[str],
) -> None:
    """Suma una sugerencia (y si fue usada después) a la capacidad ``name``."""
    d = by_cap.setdefault(name, {"hinted": 0, "used_after": 0, "measurable": True})
    d["hinted"] += 1
    key, kind = _resolve(name, agent_types)
    if kind == "unmeasurable":
        d["measurable"] = False
        return
    ts_list = (mcp if kind == "mcp" else agents).get(key, [])
    if _used_after(ts_list, hint_ts):
        d["used_after"] += 1


def report(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Calcula el uplift por capacidad y global a partir de los eventos."""
    hints = [e for e in events if e.get("event") == "capability_hint"]
    mcp, agents, agent_types = _usage_index(events)

    by_cap: dict[str, dict[str, Any]] = {}
    no_ts = 0
    for h in hints:
        hts = h.get("ts", "")
        if not hts:
            no_ts += 1
        for name in h.get("hinted", []):
            _accumulate(by_cap, name, hts, mcp, agents, agent_types)

    for d in by_cap.values():
        if d["measurable"] and d["hinted"]:
            d["rate"] = round(d["used_after"] / d["hinted"], 2)

    return _finalize(hints, by_cap, no_ts)


_LOW_NOTE = (
    "Uplift fiable necesita ≥20 hints CON ts (los viejos sin ts no se miden) y "
    "comparación con una línea base sin enrutador. Skills/commands no tienen telemetría "
    "de invocación → no medibles aquí (solo MCP tools y agentes)."
)


def _finalize(hints: list[dict[str, Any]], by_cap: dict[str, dict[str, Any]], no_ts: int) -> dict[str, Any]:
    """Arma el resumen final + nota de suficiencia de datos."""
    measurable_hints = sum(d["hinted"] for d in by_cap.values() if d["measurable"])
    sufficiency = "ok" if (len(hints) >= 20 and no_ts == 0) else "low"
    return {
        "total_hints": len(hints),
        "hints_without_ts": no_ts,
        "by_capability": by_cap,
        "data_sufficiency": sufficiency,
        "note": _LOW_NOTE if sufficiency == "low" else f"{measurable_hints} hints medibles.",
    }


def render(data: dict[str, Any]) -> str:
    """Reporte legible del uplift."""
    L = ["ENRUTADOR — UPLIFT DE HINTS (paso 4: medir)", ""]
    L.append(f"  total capability_hint: {data['total_hints']}  (sin ts: {data['hints_without_ts']})")
    L.append(f"  suficiencia de datos : {data['data_sufficiency']}")
    L.append("")
    L.append("  por capacidad (sugerida → usada después):")
    for name, d in sorted(data["by_capability"].items(), key=lambda kv: -kv[1]["hinted"]):
        if d["measurable"]:
            L.append(f"      {name:36s} {d['used_after']}/{d['hinted']}  (rate {d.get('rate', 0)})")
        else:
            L.append(f"      {name:36s} {d['hinted']} hints · no medible (skill/command)")
    L.append("")
    L.append(f"  {data['note']}")
    return "\n".join(L)


def main(argv: list[str]) -> int:
    """CLI del medidor."""
    data = report(load_events())
    if "--json" in argv:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    print(render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
