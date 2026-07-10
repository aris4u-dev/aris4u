#!/usr/bin/env python3
"""Hit-rate de ADOPCIÓN del enrutador (Fase 4 — el reporte que cierra GAP6).

Lee el event log y, a partir de los pares explícitos ``capability_adopted`` /
``capability_ignored`` emitidos por ``tools/capability_adoption.py``, calcula el hit-rate
real: de las veces que el enrutador SUGIRIÓ una capacidad y el turno se cerró, ¿cuántas el
modelo realmente USÓ?

  hit-rate = adopted / (adopted + ignored)      # solo hints RESUELTOS (cerrados)

A diferencia de ``capability_hint_report.py`` (que infiere el uso correlacionando
timelines), aquí la señal es DIRECTA: cada adopted/ignored es un hint observado de cerca
en su sesión. Desglosa por capacidad y por intención F1. Con pocos datos lo dice honesto
(``data_sufficiency=low``).

Uso:
    python3 tools/conductor_stats.py
    python3 tools/conductor_stats.py --json
    python3 tools/conductor_stats.py --log /ruta/al/events.jsonl
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ARIS_ROOT = Path(__file__).resolve().parent.parent
EVENTS_LOG = ARIS_ROOT / "logs" / "v16.1-events.jsonl"

# Mínimo de hints resueltos para considerar el hit-rate fiable.
_MIN_RESOLVED = 20


def load_events(path: Path | None = None) -> list[dict[str, Any]]:
    """Carga el event log JSONL (líneas inválidas se ignoran). Fail-open a []."""
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


def _blank() -> dict[str, int]:
    """Acumulador vacío para un grupo (capacidad o intención)."""
    return {"adopted": 0, "ignored": 0}


def _rate(d: dict[str, Any]) -> float | None:
    """Hit-rate de un grupo (None si no hay hints resueltos)."""
    resolved = d["adopted"] + d["ignored"]
    return round(d["adopted"] / resolved, 3) if resolved else None


def report(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Calcula hit-rate global, por capacidad y por intención a partir de los eventos."""
    by_cap: dict[str, dict[str, Any]] = {}
    by_intent: dict[str, dict[str, Any]] = {}
    totals = _blank()
    n_hint = 0

    for e in events:
        ev = e.get("event")
        if ev == "capability_hint":
            n_hint += 1
            continue
        if ev == "capability_adopted":
            key = "adopted"
        elif ev == "capability_ignored":
            key = "ignored"
        else:
            continue
        name = str(e.get("name", "") or "?")
        intent = str(e.get("intent", "") or "?")
        by_cap.setdefault(name, _blank())[key] += 1
        by_intent.setdefault(intent, _blank())[key] += 1
        totals[key] += 1

    for d in by_cap.values():
        d["rate"] = _rate(d)
    for d in by_intent.values():
        d["rate"] = _rate(d)

    resolved = totals["adopted"] + totals["ignored"]
    return {
        "total_hints": n_hint,
        "total_adopted": totals["adopted"],
        "total_ignored": totals["ignored"],
        "resolved": resolved,
        "pending_or_open": max(0, n_hint - resolved),
        "overall_rate": _rate(totals),
        "by_capability": by_cap,
        "by_intent": by_intent,
        "data_sufficiency": "ok" if resolved >= _MIN_RESOLVED else "low",
    }


def _fmt_rate(r: float | None) -> str:
    """Formatea un hit-rate (``n/a`` si None)."""
    return f"{r:.0%}" if isinstance(r, float) else "n/a"


def render(data: dict[str, Any]) -> str:
    """Reporte legible del hit-rate de adopción."""
    L = ["CONDUCTOR — HIT-RATE DE ADOPCIÓN (Fase 4)", ""]
    L.append(f"  capability_hint    : {data['total_hints']}")
    L.append(
        f"  resueltos          : {data['resolved']} "
        f"(adopted {data['total_adopted']} / ignored {data['total_ignored']})"
    )
    L.append(f"  pendientes/abiertos: {data['pending_or_open']}")
    L.append(f"  hit-rate global    : {_fmt_rate(data['overall_rate'])}")
    L.append(f"  suficiencia datos  : {data['data_sufficiency']}")
    L.append("")
    L.append("  por intención (adopted/resueltos · rate):")
    for intent, d in sorted(data["by_intent"].items(), key=lambda kv: -(kv[1]["adopted"] + kv[1]["ignored"])):
        res = d["adopted"] + d["ignored"]
        L.append(f"      {intent:16s} {d['adopted']}/{res}  ({_fmt_rate(d['rate'])})")
    L.append("")
    L.append("  por capacidad (adopted/resueltos · rate):")
    for name, d in sorted(data["by_capability"].items(), key=lambda kv: -(kv[1]["adopted"] + kv[1]["ignored"])):
        res = d["adopted"] + d["ignored"]
        L.append(f"      {name:40s} {d['adopted']}/{res}  ({_fmt_rate(d['rate'])})")
    if data["data_sufficiency"] == "low":
        L.append("")
        L.append(
            f"  NOTA: hit-rate fiable necesita ≥{_MIN_RESOLVED} hints resueltos "
            "(adopted+ignored). Los hints de un turno se resuelven en Stop; sesiones aún "
            "abiertas quedan en 'pendientes'."
        )
    return "\n".join(L)


def _arg(argv: list[str], flag: str) -> str | None:
    """Valor de ``--flag valor`` en argv, o None."""
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main(argv: list[str]) -> int:
    """CLI del reporte de hit-rate de adopción."""
    log = _arg(argv, "--log")
    data = report(load_events(Path(log) if log else None))
    if "--json" in argv:
        print(json.dumps(data, indent=2, ensure_ascii=False))
        return 0
    print(render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
