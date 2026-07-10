#!/usr/bin/env python3
"""Vigía de fuentes de Claude para la auto-adaptación de ARIS4U (Paso 7b).

Compara el estado ACTUAL de las fuentes que definen el contrato con Claude contra
el último estado visto (data/adapt_state.json) y reporta qué cambió, clasificando
cada delta en 'mechanical' (auto-aplicable con gate) o 'semantic' (necesita PR).
NO modifica el repo — solo detecta y clasifica. La actuación la hace run_daily.

Fuentes vigiladas:
  - claude --version            -> versión del harness
  - ~/.claude/cache/changelog.md-> hash (cambios del harness; interpretación = semántica)
  - ~/.claude/settings.json     -> hash (nuestro cableado de hooks/env)
  - engine.v16.config.CLAUDE_MODEL -> id de modelo activo

Uso:
  python tools/adapt/watch_sources.py            # detecta y reporta JSON (no actualiza estado)
  python tools/adapt/watch_sources.py --update   # además fija el estado actual como baseline
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ARIS4U_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT))
STATE_FILE = ROOT / "data" / "adapt_state.json"

# Clasificación determinista por fuente. 'mechanical' = cambio acotado y auto-verificable;
# 'semantic' = requiere interpretar texto -> Claude headless abre PR (HITL).
ROUTE = {
    "claude_version": "mechanical",   # bump de versión -> re-correr gate + sync de paths/model
    "claude_model": "mechanical",     # id de modelo -> sync determinista (manifest futuro)
    "settings_hash": "mechanical",    # cambió nuestro cableado -> re-validar contrato (gate)
    "changelog_hash": "semantic",     # changelog nuevo -> interpretar features/eventos (PR)
}


def _hash_file(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def current_state() -> dict:
    home = Path.home()
    try:
        ver = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        ver = ""
    try:
        from engine.v16.config import CLAUDE_MODEL
    except Exception:
        CLAUDE_MODEL = ""
    return {
        "claude_version": ver,
        "claude_model": CLAUDE_MODEL,
        "changelog_hash": _hash_file(home / ".claude" / "cache" / "changelog.md"),
        "settings_hash": _hash_file(home / ".claude" / "settings.json"),
    }


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def diff(prev: dict, cur: dict) -> list[dict]:
    deltas = []
    for key, route in ROUTE.items():
        old, new = prev.get(key, ""), cur.get(key, "")
        if old != new:
            deltas.append({
                "source": key,
                "route": route,
                "old": old or "(baseline)",
                "new": new,
            })
    return deltas


def main() -> int:
    cur = current_state()
    prev = load_state()
    first_run = not prev
    deltas = diff(prev, cur)
    report = {
        "changed": bool(deltas),
        "first_run": first_run,
        "deltas": deltas,
        "mechanical": [d for d in deltas if d["route"] == "mechanical"],
        "semantic": [d for d in deltas if d["route"] == "semantic"],
        "current": cur,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if "--update" in sys.argv:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(cur, indent=2))
        print(f"\n[estado fijado como baseline en {STATE_FILE}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
