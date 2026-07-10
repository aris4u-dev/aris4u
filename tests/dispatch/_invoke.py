#!/usr/bin/env python3
"""Test harness: invoca un handler del dispatcher AISLADO (sin tocar events/__init__.py).

Uso:  _invoke.py <module> <EventName>   (stdin = payload JSON)
  <module> = session_start | session_end | stop | subagent_start

Replica el contrato de dispatch.py (read_event + SystemExit passthrough), pero resuelve
el handler por import directo del módulo bajo prueba. Permite capturar stdout/stderr y
side-effects de cada handler nuevo antes de que estén registrados en HANDLERS.
"""
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
sys.path.insert(0, str(HOOKS))

from dispatch.contract import read_event  # noqa: E402


def main() -> None:
    module = sys.argv[1]
    event_name = sys.argv[2] if len(sys.argv) > 2 else ""
    payload = read_event()
    mod = __import__(f"dispatch.events.{module}", fromlist=["handle"])
    try:
        mod.handle(event_name, payload)
    except SystemExit:
        raise
    except Exception as e:  # mismo fail-open que dispatch.py, pero ruidoso en test
        print(f"[HANDLER-EXCEPTION] {e!r}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
