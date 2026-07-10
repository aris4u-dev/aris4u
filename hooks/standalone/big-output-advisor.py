#!/usr/bin/env python3
"""PostToolUse advisory (Read|Bash|Grep|Glob): si el output que acaba de entrar al
contexto es GRANDE, recuerda la vía de compresión local (cero cuota). Además lleva
un contador POR SESIÓN: cuando se acumulan varias lecturas grandes en el mismo hilo,
escala a la regla H3 (delegar el VOLUMEN a subagentes Sonnet, no absorberlo en el
hilo top). Fail-open, nunca bloquea.

Portabilidad: STATE_DIR usa Path.home()/.claude/state (estándar en cualquier install).
Sin paths hardcodeados. Fuente versionada: hooks/standalone/big-output-advisor.py.
"""
import json
import sys
from pathlib import Path

THRESHOLD = 30000  # chars ≈ 7.5k tokens de una sola lectura
ESCALATE_AT = 3    # lecturas grandes acumuladas en el hilo -> escala a H3
STATE_DIR = Path.home() / ".claude" / "state"


def _state_path(session_id: str) -> Path:
    sid = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "default"
    return STATE_DIR / f"volume-{sid}.json"


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"count": 0, "kb": 0}


def _save(p: Path, st: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(st), encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    try:
        d = json.load(sys.stdin)
    except Exception:
        return 0
    out = d.get("tool_response")
    text = out if isinstance(out, str) else json.dumps(out) if out else ""
    if len(text) <= THRESHOLD:
        return 0

    kb = len(text) // 1024
    p = _state_path(str(d.get("session_id") or ""))
    st = _load(p)
    st["count"] = int(st.get("count", 0)) + 1
    st["kb"] = int(st.get("kb", 0)) + kb

    if st["count"] >= ESCALATE_AT:
        msg = (
            f"⚠️ H3 (model-governance.md): ya van {st['count']} lecturas grandes "
            f"(~{st['kb']} KB) absorbidas por el HILO top en esta sesión. El hilo NO "
            "debe cargar volumen crudo — delega las próximas lecturas/exploración a un "
            "subagente Sonnet (`Agent(model=\"sonnet\")`) y recibe solo el digest, o usa "
            "`~/.claude/bin/local-digest.sh <archivo>` (cero cuota)."
        )
        st["count"] = 0  # re-acumula, no spamear cada lectura posterior
    else:
        msg = (
            f"📦 output grande (~{kb} KB) acaba de entrar al contexto. Para próximas "
            "lecturas de este volumen: `~/.claude/bin/local-digest.sh <archivo>` "
            "(resumen local, cero cuota) o delega a un subagente Sonnet y recibe el digest."
        )
    _save(p, st)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": msg,
    }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
