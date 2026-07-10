#!/usr/bin/env python3
"""Registrar la UTILIDAD de una llamada F1 (aris_structure/aris_critique) — datos para F2.

Uso: python tools/f1_feedback.py <call_id> <ok|no> [nota...]

Marca si la salida del amplificador local AYUDÓ de verdad. SIN auto-etiquetado: lo decides
tú o Claude, no el sistema (honesto por diseño). El call_id aparece en el footer de cada
salida F1 ("F1 id:xxxxxxxx"). Los pares (llamada, útil) los lee tools/f1_roi.py y, cuando
hay suficientes, alimentan engine/v16/orchestration/calibration.py (§8.5).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, UTC
from pathlib import Path

ROOT = Path(os.environ.get("ARIS4U_ROOT") or Path(__file__).resolve().parents[1])
EVENTS_LOG = ROOT / "logs" / "v16.1-events.jsonl"

_TRUTHY = {"ok", "si", "sí", "yes", "y", "1", "true", "util", "útil", "useful"}
_FALSY = {"no", "n", "0", "false", "inutil", "inútil", "useless"}


def record_feedback(log_path: Path, call_id: str, useful: bool, note: str = "") -> dict:
    """Anexa un evento f1_feedback al log JSONL. Devuelve el evento escrito.

    Args:
        log_path: Ruta del log JSONL (logs/v16.1-events.jsonl).
        call_id: Id de la llamada F1 a etiquetar.
        useful: True si la salida F1 ayudó.
        note: Comentario libre opcional.

    Returns:
        El evento escrito (dict).
    """
    event = {
        "ts": datetime.now(UTC).isoformat(),
        "hook": "f1_feedback",
        "event": "f1_feedback",
        "call_id": call_id,
        "useful": bool(useful),
        "note": note,
        "session_id": os.environ.get("ARIS4U_SESSION_ID", ""),
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def parse_useful(token: str) -> bool:
    """Convierte un token de CLI (ok/no/si/…) a booleano. Lanza SystemExit si es inválido."""
    t = token.strip().lower()
    if t in _TRUTHY:
        return True
    if t in _FALSY:
        return False
    raise SystemExit(f"valor de utilidad inválido: {token!r} (usa ok|no)")


def main(argv: list[str]) -> int:
    """Entrypoint CLI."""
    if len(argv) < 2:
        print(__doc__)
        return 2
    call_id = argv[0]
    useful = parse_useful(argv[1])
    note = " ".join(argv[2:])
    ev = record_feedback(EVENTS_LOG, call_id, useful, note)
    suffix = f" — {note}" if note else ""
    print(f"registrado: {call_id} útil={ev['useful']}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
