#!/usr/bin/env python3
"""Etiquetado SIN FRICCIÓN de las llamadas del amplificador local F1 (aris_structure/critique).

El cuello de botella para activar la capa de decisión (§8.5) es juntar 30 llamadas ETIQUETADAS
(útil/no). Hacerlo a mano con `f1_feedback.py <call_id> ok|no` obliga a cazar el call_id en los
logs → casi nunca se llega a 30. Esta herramienta lo vuelve un paso:

  python tools/f1_label.py                 # lista las llamadas PENDIENTES (con índice) + progreso N/30
  python tools/f1_label.py 1 ok            # etiqueta la #1 (la más reciente) como útil
  python tools/f1_label.py 3 no "vago"     # etiqueta la #3 como no-útil, con nota
  python tools/f1_label.py <call_id> ok    # también acepta el call_id del footer "F1 id:xxxx"

Reusa `f1_roi` (lectura + ROI) y `f1_feedback` (escritura). Cuando llega a 30, `f1_roi` marca
``ready_for_calibration`` y la calibración puede validar el sensor.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path

ROOT = Path(os.environ.get("ARIS4U_ROOT") or Path(__file__).resolve().parents[1])
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.f1_feedback import parse_useful, record_feedback  # noqa: E402
from tools.f1_roi import _F1_TOOLS, _MIN_LABELS, compute_roi, read_events  # noqa: E402

EVENTS_LOG = ROOT / "logs" / "v16.1-events.jsonl"


def _labeled_ids(events: list[dict]) -> set[str]:
    """call_ids que YA tienen feedback (para no re-pedirlos)."""
    return {e["call_id"] for e in events
            if e.get("event") == "f1_feedback" and e.get("call_id")}


def pending_calls(events: list[dict]) -> list[dict]:
    """Llamadas F1 etiquetables y aún sin etiqueta, de la más reciente a la más vieja.

    Etiquetable = es una tool F1, el cuerpo respondió (``available``) y tiene ``call_id``
    (las llamadas frías/omitidas no aportan señal y no se listan).
    """
    done = _labeled_ids(events)
    calls = [e for e in events
             if e.get("event") == "mcp_tool" and e.get("tool") in _F1_TOOLS
             and e.get("available") and e.get("call_id") and e["call_id"] not in done]
    return sorted(calls, key=lambda c: c.get("ts", ""), reverse=True)


def _age(ts: str) -> str:
    """'hace 3h' / 'hace 2d' a partir de un timestamp ISO (o '' si no parsea)."""
    try:
        then = datetime.fromisoformat(ts)
        secs = (datetime.now(UTC) - then).total_seconds()
    except (ValueError, TypeError):
        return ""
    if secs < 3600:
        return f"hace {int(secs // 60)}min"
    if secs < 86400:
        return f"hace {int(secs // 3600)}h"
    return f"hace {int(secs // 86400)}d"


def _progress(events: list[dict]) -> str:
    """Línea de progreso hacia el umbral de calibración."""
    roi = compute_roi(events)
    n = roi["labeled"]
    if roi["ready_for_calibration"]:
        return f"✅ {n}/{_MIN_LABELS} etiquetas — LISTO para calibrar (sensor_is_predictive)."
    return f"⏳ {n}/{_MIN_LABELS} etiquetas · útiles {roi['useful']}/{n or 0} · faltan {_MIN_LABELS - n}."


def list_pending(events: list[dict]) -> int:
    """Imprime las pendientes con índice + contexto + el progreso. Siempre devuelve 0."""
    pend = pending_calls(events)
    print(_progress(events))
    if not pend:
        print("\nNo hay llamadas pendientes de etiquetar. Usa el amplificador "
              "(aris_structure/aris_critique) en tu trabajo y vuelve a marcar.")
        return 0
    print(f"\nPendientes ({len(pend)}) — etiqueta con:  f1_label.py <#> ok|no\n")
    for i, c in enumerate(pend, 1):
        backend = c.get("backend", "?")
        chars = c.get("chars", "?")
        print(f"  {i:>2}. {c['tool']:<14} {_age(c.get('ts','')):<9} "
              f"{backend} · {chars} chars · id:{c['call_id']}")
    return 0


def label(events: list[dict], selector: str, useful: bool, note: str,
          log_path: Path = EVENTS_LOG) -> int:
    """Etiqueta una llamada por índice (de la lista) o por call_id. Devuelve exit code."""
    pend = pending_calls(events)
    if selector.isdigit():
        idx = int(selector)
        if not 1 <= idx <= len(pend):
            print(f"índice {idx} fuera de rango (hay {len(pend)} pendientes). "
                  "Corre sin args para ver la lista.", file=sys.stderr)
            return 1
        call_id = pend[idx - 1]["call_id"]
    else:
        call_id = selector  # call_id directo (del footer "F1 id:xxxx")
    record_feedback(log_path, call_id, useful, note)
    suffix = f" — {note}" if note else ""
    print(f"registrado: id:{call_id} útil={useful}{suffix}")
    # progreso fresco (re-lee para incluir la etiqueta recién escrita)
    print(_progress(read_events(log_path)))
    return 0


def main(argv: list[str]) -> int:
    """Entrypoint CLI: sin args = listar; <selector> <ok|no> [nota] = etiquetar."""
    events = read_events(EVENTS_LOG)
    if not argv:
        return list_pending(events)
    if len(argv) < 2:
        print(__doc__)
        return 2
    return label(events, argv[0], parse_useful(argv[1]), " ".join(argv[2:]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
