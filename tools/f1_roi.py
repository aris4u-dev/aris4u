#!/usr/bin/env python3
"""Reporte de ROI del amplificador local F1 (aris_structure/aris_critique) — blueprint F2.

Lee logs/v16.1-events.jsonl y reporta: nº de llamadas F1, tasa de disponibilidad del
cuerpo local (MLX vivo), latencia (p50/p90), y — cuando hay feedback registrado con
tools/f1_feedback.py — la tasa de UTILIDAD. Cuando haya suficientes etiquetas, los pares
(llamada, útil) alimentan engine/v16/orchestration/calibration.py para validar el sensor
(§8.5). Mientras tanto: descriptivo, honesto, sin inventar señales.

Uso: python tools/f1_roi.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(os.environ.get("ARIS4U_ROOT") or Path(__file__).resolve().parents[1])
EVENTS_LOG = ROOT / "logs" / "v16.1-events.jsonl"
_F1_TOOLS = ("aris_structure", "aris_critique")
_MIN_LABELS = 30  # umbral mínimo de etiquetas para considerar la calibración fiable


def _percentile(values: list[float], pct: float) -> float:
    """Percentil lineal (pct ∈ [0,1]). 0.0 si la lista está vacía."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _is_f1_call(e: dict) -> bool:
    """True si el evento es una invocación de una tool F1."""
    return e.get("event") == "mcp_tool" and e.get("tool") in _F1_TOOLS


def _feedback_map(events: list[dict]) -> dict[str, bool]:
    """Mapa call_id → útil a partir de los eventos f1_feedback."""
    out: dict[str, bool] = {}
    for e in events:
        if e.get("event") == "f1_feedback" and e.get("call_id"):
            out[e["call_id"]] = bool(e["useful"])
    return out


def _summarize(calls: list[dict], feedback: dict[str, bool]) -> dict:
    """Recorre las llamadas una sola vez y agrega disponibilidad/latencia/utilidad."""
    available, latencies, labeled = [], [], 0
    useful_n = 0
    by_tool: dict[str, int] = {}
    for c in calls:
        by_tool[c["tool"]] = by_tool.get(c["tool"], 0) + 1
        if not c.get("available"):
            continue
        available.append(c)
        if "latency_ms" in c:
            latencies.append(float(c["latency_ms"]))
        cid = c.get("call_id")
        if cid in feedback:
            labeled += 1
            useful_n += int(feedback[cid])
    return {
        "available": available, "latencies": latencies,
        "labeled": labeled, "useful_n": useful_n, "by_tool": by_tool,
    }


def compute_roi(events: list[dict]) -> dict:
    """Métricas de ROI de F1 a partir de eventos del log (función pura, testable).

    Args:
        events: Lista de eventos del log JSONL (mcp_tool + f1_feedback).

    Returns:
        Dict con conteos, disponibilidad, latencia, utilidad y flag de calibración.
    """
    calls = [e for e in events if _is_f1_call(e)]
    s = _summarize(calls, _feedback_map(events))
    n_avail, n_lab = len(s["available"]), s["labeled"]
    return {
        "total_calls": len(calls),
        "available": n_avail,
        "unavailable": len(calls) - n_avail,
        "availability_rate": (n_avail / len(calls)) if calls else 0.0,
        "by_tool": s["by_tool"],
        "latency_p50_ms": _percentile(s["latencies"], 0.5),
        "latency_p90_ms": _percentile(s["latencies"], 0.9),
        "labeled": n_lab,
        "useful": s["useful_n"],
        "useful_rate": (s["useful_n"] / n_lab) if n_lab else 0.0,
        "ready_for_calibration": n_lab >= _MIN_LABELS,
    }


def read_events(log_path: Path) -> list[dict]:
    """Lee y parsea el log JSONL (líneas corruptas se ignoran). [] si no existe."""
    if not log_path.exists():
        return []
    out: list[dict] = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def calibration_data(events: list[dict]) -> tuple[list[float], list[float]]:
    """(scores, éxitos) de las llamadas F1 etiquetadas que TIENEN promise_score (§8.5)."""
    feedback = _feedback_map(events)
    scores: list[float] = []
    successes: list[float] = []
    for e in events:
        if (_is_f1_call(e) and e.get("call_id") in feedback
                and isinstance(e.get("promise_score"), (int, float))):
            scores.append(float(e["promise_score"]))
            successes.append(1.0 if feedback[e["call_id"]] else 0.0)
    return scores, successes


def run_calibration(events: list[dict]) -> dict:
    """Corre el gate §8.5 (sensor_is_predictive) sobre los pares (promise_score, útil) reales.

    Es el DISPARADOR que faltaba: cuando hay ≥30 llamadas etiquetadas con score, valida si
    el promise_score del cuerpo predice la utilidad real → decide si cablear la capa de decisión.

    Returns:
        ``{ran: False, reason}`` si faltan datos, o ``{ran, predictive, reason, auc, n}``.
    """
    scores, successes = calibration_data(events)
    if len(scores) < _MIN_LABELS:
        return {"ran": False,
                "reason": f"{len(scores)}/{_MIN_LABELS} llamadas etiquetadas CON promise_score"}
    import numpy as np
    from engine.v16.orchestration.calibration import sensor_is_predictive
    v = sensor_is_predictive(np.asarray(scores), np.asarray(successes))
    return {"ran": True, "predictive": bool(v.predictive), "reason": v.reason,
            "auc": float(v.auc), "n": int(v.n)}


def format_report(roi: dict) -> str:
    """Formatea el reporte de ROI para CLI."""
    lines = [
        "=== ROI del amplificador local F1 (blueprint F2) ===",
        f"Llamadas F1: {roi['total_calls']}  (por tool: {roi['by_tool']})",
        f"Disponibilidad MLX: {roi['available']}/{roi['total_calls']} "
        f"({roi['availability_rate'] * 100:.0f}%)  ·  omitidas (frío): {roi['unavailable']}",
        f"Latencia: p50 {roi['latency_p50_ms']:.0f}ms · p90 {roi['latency_p90_ms']:.0f}ms",
        f"Feedback: {roi['labeled']} etiquetadas · útiles {roi['useful']} "
        f"({roi['useful_rate'] * 100:.0f}%)",
    ]
    if roi["ready_for_calibration"]:
        lines.append("✅ Suficientes etiquetas → listo para calibración (calibration.py).")
    else:
        lines.append(
            f"⏳ Faltan etiquetas para calibrar ({roi['labeled']}/{_MIN_LABELS}). "
            "Marca utilidad con tools/f1_feedback.py <call_id> ok|no."
        )
    return "\n".join(lines)


def main() -> int:
    """Entrypoint CLI."""
    events = read_events(EVENTS_LOG)
    print(format_report(compute_roi(events)))
    cal = run_calibration(events)
    if cal["ran"]:
        verdict = ("PREDICTIVO → cablear la capa de decisión" if cal["predictive"]
                   else "NO predictivo (teatro) → NO cablear")
        print(f"§8.5 calibración (n={cal['n']}, AUC={cal['auc']:.3f}): {verdict}\n  {cal['reason']}")
    else:
        print(f"§8.5 calibración: aún no corre — {cal['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
