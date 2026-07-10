#!/usr/bin/env python3
"""SessionStart: recordatorio H1 de gobierno de modelos (model-governance.md).

Desde 2026-07-04 (tarde) Opus 4.8 es el modelo de hilo FIJO por defecto (decisión
del usuario: Sonnet 5 como hilo consumía el contexto de sesión demasiado rápido) —
ya no se sugiere bajar a Sonnet por default. Solo Fable como hilo sigue
disparando el recordatorio de que es tier reservado a estrategia/plan maestro.
Advisory puro — exit 0 siempre, fail-open ante cualquier error.

Portabilidad: lee settings.json desde Path.home()/.claude/ (estándar). Sin paths
hardcodeados. Fuente versionada: hooks/standalone/session-tier-reminder.py.
"""
import json
import sys
from pathlib import Path


def _session_model(data: dict) -> str:
    raw = ""
    model = data.get("model")
    if isinstance(model, dict):
        raw = str(model.get("display_name") or model.get("id") or "")
    if not raw:
        try:
            p = Path.home() / ".claude" / "settings.json"
            raw = json.loads(p.read_text(encoding="utf-8")).get("model", "")
        except Exception:
            raw = ""
    return raw.lower()


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        data = {}
    m = _session_model(data)
    if "fable" in m:
        print(
            "🧵 HILO FABLE (H1/H4) — $10/$50, 2× Opus, MÁS CARO. "
            "Fable NO es default: solo plan maestro/decisión irreversible. "
            "Vía correcta: hilo Opus + Agent(model=\"fable\") puntual → gate → ejecutar. "
            "Si la 1ª tarea NO es plan maestro, dilo y sugiere relanzar en Opus. "
            "Disciplina: `model-discipline-report.py`."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
