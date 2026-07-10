#!/usr/bin/env python3
"""Reporte de costo/routing de subagentes (V18 Fase C).

Lee el event log (`agent_dispatched` + `model_hint`) y mide la DISCIPLINA DE ROUTING:
qué fracción de los `Agent()` especificó `model=` explícito (vs heredó el hilo — hoy
Fable, el error #1 que V18 combate), la distribución por modelo, y un costo RELATIVO
estimado (por-llamada ponderada; no hay token counts por-subagente desde los hooks).

Uso:
    python3 tools/cost_report.py                 # ventana últimos 7 días, salida humana
    python3 tools/cost_report.py --days 14
    python3 tools/cost_report.py --all --json     # todo el log, JSON

Nota honesta: el costo es una PROXY por-llamada (peso relativo por tier de precio de
salida), no dólares reales — los hooks no exponen tokens por subagente. La señal valiosa
es la DISCIPLINA (% con model= explícito) y la distribución, no el número absoluto.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional
from collections.abc import Iterator

# Permite `from tools.model_router import …` tanto importado como corrido de script.
_REPO_ROOT = Path(os.environ.get("ARIS4U_ROOT") or Path(__file__).resolve().parents[1])
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Peso relativo por tier (≈ precio de tokens de salida normalizado a sonnet=1.0).
# opus/fable = premium; haiku = barato. Ajustable si cambian las tarifas.
_COST_WEIGHT: dict[str, float] = {
    "opus": 5.0,
    "fable": 5.0,
    "sonnet": 1.0,
    "haiku": 0.27,
}


def _default_log() -> Path:
    root = Path(os.environ.get("ARIS4U_ROOT") or Path(__file__).resolve().parents[1])
    return Path(os.environ.get("ARIS4U_LOG_FILE") or (root / "logs" / "v16.1-events.jsonl"))


def _norm_model(raw: Optional[str]) -> Optional[str]:
    """Normaliza un model_param a tier corto (opus/sonnet/haiku/fable) o None si heredó."""
    if not raw:
        return None
    low = str(raw).lower()
    for name in ("opus", "sonnet", "haiku", "fable"):
        if name in low:
            return name
    return None


def _iter_events(log_path: Path, since: Optional[datetime]) -> Iterator[dict]:
    """Itera eventos JSONL (fail-open por línea). Filtra por ts >= since si se da."""
    try:
        with open(log_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if since is not None:
                    ts = ev.get("ts", "")
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if when < since:
                            continue
                    except Exception:
                        pass
                yield ev
    except OSError:
        return


def compute_report(log_path: Path, since: Optional[datetime], session_model: str = "") -> dict:
    """Agrega el reporte de routing/costo desde el event log.

    Args:
        log_path: Ruta al event log JSONL.
        since: Solo eventos con ts >= since (None = todo).
        session_model: Modelo del hilo (para atribuir el costo de los heredados).

    Returns:
        dict con dispatches, by_model, inherited, discipline_pct, cost_units, by_intent.
    """
    by_model: Counter[str] = Counter()   # model= explícito por tier
    inherited = 0                        # Agent() sin model= (heredó el hilo)
    by_subagent: Counter[str] = Counter()
    by_intent: Counter[str] = Counter()  # de model_hint events
    total = 0

    sess = _norm_model(session_model) or "sonnet"
    for ev in _iter_events(log_path, since):
        etype = ev.get("event")
        if etype == "agent_dispatched":
            total += 1
            m = _norm_model(ev.get("model_param"))
            if m is None:
                inherited += 1
            else:
                by_model[m] += 1
            by_subagent[ev.get("subagent_type", "unknown")] += 1
        elif etype == "model_hint":
            by_intent[ev.get("intent", "unknown")] += 1

    explicit = total - inherited
    discipline = round(100.0 * explicit / total, 1) if total else 0.0

    # Costo relativo: explícitos por su tier; heredados al tier del hilo de sesión.
    cost_units = sum(_COST_WEIGHT.get(m, 1.0) * n for m, n in by_model.items())
    cost_units += _COST_WEIGHT.get(sess, 1.0) * inherited

    return {
        "dispatches": total,
        "explicit_model": explicit,
        "inherited": inherited,
        "discipline_pct": discipline,
        "session_model": sess,
        "by_model": dict(by_model),
        "by_subagent": dict(by_subagent.most_common(10)),
        "by_intent": dict(by_intent),
        "cost_units_relative": round(cost_units, 1),
        "cost_note": "unidades relativas por-llamada (opus/fable=5·sonnet=1·haiku=0.27), no dólares",
    }


def format_report(r: dict) -> str:
    lines = ["=== ARIS4U ROUTING / COSTO (V18) ==="]
    lines.append(f"Agent() despachados: {r['dispatches']}  ·  con model= explícito: "
                 f"{r['explicit_model']}  ·  heredados: {r['inherited']}")
    lines.append(f"DISCIPLINA DE ROUTING: {r['discipline_pct']}%  "
                 f"(meta V18: >90% con model= explícito)")
    if r["inherited"]:
        lines.append(f"⚠️ {r['inherited']} Agent() heredaron el hilo (={r['session_model']}) — "
                     f"cada uno debió llevar model= explícito")
    if r["by_model"]:
        dist = " · ".join(f"{k}={v}" for k, v in sorted(r["by_model"].items()))
        lines.append(f"Por modelo (explícitos): {dist}")
    if r["by_intent"]:
        dist = " · ".join(f"{k}={v}" for k, v in sorted(r["by_intent"].items()))
        lines.append(f"Intención (model_hint): {dist}")
    lines.append(f"Costo relativo estimado: {r['cost_units_relative']} unidades "
                 f"({r['cost_note']})")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Reporte de routing/costo de subagentes (V18)")
    ap.add_argument("--days", type=int, default=7, help="ventana en días (default 7)")
    ap.add_argument("--all", action="store_true", help="ignorar la ventana temporal")
    ap.add_argument("--json", action="store_true", help="salida JSON")
    ap.add_argument("--log", default=None, help="ruta alterna del event log")
    ns = ap.parse_args(argv)

    log_path = Path(ns.log) if ns.log else _default_log()
    since = None if ns.all else datetime.now(UTC) - timedelta(days=ns.days)
    # Modelo de sesión para atribuir heredados (best-effort desde settings.json).
    sess = ""
    try:
        from tools.model_router import session_model as _sm
        sess = _sm()
    except Exception:
        sess = ""
    r = compute_report(log_path, since, session_model=sess)
    print(json.dumps(r, indent=2, ensure_ascii=False) if ns.json else format_report(r))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
