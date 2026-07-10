#!/usr/bin/env python3
"""ARIS4U hook dispatcher — un entrypoint único por evento de Claude Code.

Uso:  dispatch.py <EventName>      (stdin = payload JSON del evento)

Reemplaza los N hooks .sh por evento por un solo proceso python que resuelve el
handler y aplica el contrato (advisory / bloqueo). Fail-open: cualquier error de
infraestructura → exit 0 (nunca rompe el tool ni bloquea silenciosamente).

Eventos migrados: ver hooks/dispatch/events/__init__.py (HANDLERS). Un evento sin
handler cae a passthrough (no-op), de modo que la migración es incremental y segura.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Permite `import dispatch.*` ejecutando este archivo directamente desde settings.json.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dispatch.contract import passthrough, read_event  # noqa: E402
from dispatch.events import HANDLERS  # noqa: E402


def main() -> None:
    event_name = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = read_event()
    # Expone el session_id del payload de Claude Code a los handlers vía env:
    # la telemetría auto_recall lo lee de ARIS4U_SESSION_ID para que el medidor
    # recall_usefulness pueda localizar el transcript de la sesión. Antes quedaba
    # vacío → los recalls salían "sin instrumentar". Fail-safe: solo setea si viene.
    _sid = payload.get("session_id")
    if _sid:
        os.environ["ARIS4U_SESSION_ID"] = str(_sid)
    handler = HANDLERS.get(event_name)
    if handler is None:
        passthrough()  # evento aún no migrado → no-op
    try:
        handler(event_name, payload)
    except SystemExit:
        raise  # las funciones del contrato salen con sys.exit; respetarlo
    except Exception:
        passthrough()  # fail-open ante cualquier error del handler


if __name__ == "__main__":
    main()
