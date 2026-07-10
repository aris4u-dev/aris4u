"""Helper compartido: leer el intervalo de un transcript de agente (agent-*.jsonl).

Usado por ``capacity_advisor`` (μ + llegadas) y ``concurrency_governor`` (μ). Antes el
parseo estaba DUPLICADO en ambos; se unifica aquí. Solo stdlib (json/datetime) — no arrastra
numpy, para que el hot-path del gobernador siga barato.
"""

from __future__ import annotations

import json
from datetime import datetime


def agent_span(path: str) -> tuple[datetime, datetime] | None:
    """(inicio, fin) de un agent-*.jsonl = min/max de sus timestamps; None si hay < 2."""
    stamps: list[datetime] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                raw = json.loads(line).get("timestamp")
            except (json.JSONDecodeError, AttributeError):
                continue
            if raw:
                stamps.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    if len(stamps) < 2:
        return None
    return min(stamps), max(stamps)
