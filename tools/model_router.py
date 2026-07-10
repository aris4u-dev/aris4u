#!/usr/bin/env python3
"""Router de modelos Claude para subagentes (V18 Fase A).

Decide QUÉ modelo Claude (opus/sonnet/haiku) debe usar un subagente `Agent()` según
la dificultad COGNITIVA de su subtarea — NO su volumen. Materializa la doctrina
tri-modelo de `~/.claude/rules/parallel-dispatch.md §ROUTING`.

Frontera CLARA — NO confundir con `engine/v16/model_router.py`:
  - ESTE módulo (`tools/model_router.py`) rutea entre modelos CLAUDE (opus/sonnet/haiku)
    para el fan-out de subagentes. Es advisory: emite el `model=` recomendado.
  - `engine/v16/model_router.py` rutea el PERÍMETRO LOCAL (MLX/Ollama/Grok) para tareas
    de amplificación (dialectic/structure/critique/digest). Otra capa, otro propósito.

Fable 5 NUNCA es salida de este router: es el HILO de sesión, no un subagente. Un
subagente sin `model=` HEREDA el hilo (hoy Fable) → el error #1 que este router evita.

Uso:
    from tools.model_router import route_model, session_model, routing_hint
    route_model("verify")            -> "sonnet"
    route_model("synthesis")         -> "opus"
    route_model("format")            -> "haiku"
    route_model(intent="decision")   -> "opus"   (fallback por intención F1)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# ── Mapa subtarea → modelo (la tabla de la doctrina, en código) ──────────────────
# Claves = tipo COGNITIVO de la subtarea. Se normaliza a lowercase antes del lookup.
_OPUS = "opus"
_SONNET = "sonnet"
_HAIKU = "haiku"

_SUBTASK_MODEL: dict[str, str] = {
    # Opus — síntesis / juicio / lo sutil
    "synthesis": _OPUS,
    "synthesize": _OPUS,
    "judge": _OPUS,
    "verdict": _OPUS,
    "decide": _OPUS,
    "decision": _OPUS,
    "architecture": _OPUS,
    "design": _OPUS,
    "strategy": _OPUS,
    "audit": _OPUS,
    # Sonnet — el grueso del fan-out
    "verify": _SONNET,
    "verification": _SONNET,
    "review": _SONNET,
    "search": _SONNET,
    "explore": _SONNET,
    "exploration": _SONNET,
    "read": _SONNET,
    "summarize": _SONNET,
    "summary": _SONNET,
    "transform": _SONNET,
    "edit": _SONNET,
    "implement": _SONNET,
    "implementation": _SONNET,
    "extract_structured": _SONNET,
    "research": _SONNET,
    # Haiku — trivial mecánico
    "classify": _HAIKU,
    "classification": _HAIKU,
    "format": _HAIKU,
    "count": _HAIKU,
    "label": _HAIKU,
    "extract_field": _HAIKU,
    "extract": _HAIKU,
}

# ── Fallback por intención F1 (cuando no se conoce el tipo de subtarea) ───────────
# Nota: el fan-out de una 'decision'/'research' tiene FINDERS (sonnet) + SÍNTESIS (opus).
# El fallback devuelve el modelo del trabajo DOMINANTE de fan-out para esa intención.
_INTENT_MODEL: dict[str, str] = {
    "decision": _OPUS,
    "research": _SONNET,      # los finders son el grueso; la síntesis final = opus explícito
    "implementation": _SONNET,
    "fix": _SONNET,
    "simple": _HAIKU,
}

_DEFAULT = _SONNET  # sin señal → el grueso (nunca heredar Fable, nunca asumir opus)


def route_model(subtask_type: Optional[str] = None, *, intent: Optional[str] = None) -> str:
    """Devuelve el `model=` recomendado (opus/sonnet/haiku) para un subagente.

    Prioridad: subtask_type (lo más específico) > intent (fallback F1) > default sonnet.
    Nunca devuelve 'fable' (es el hilo, no un subagente). Fail-open: entrada rara → sonnet.

    Args:
        subtask_type: Tipo cognitivo de la subtarea (verify/synthesis/format/…).
        intent: Intención F1 del prompt (decision/research/implementation/fix/simple).

    Returns:
        Uno de "opus" | "sonnet" | "haiku".
    """
    if subtask_type:
        key = str(subtask_type).strip().lower().replace("-", "_").replace(" ", "_")
        if key in _SUBTASK_MODEL:
            return _SUBTASK_MODEL[key]
    if intent:
        key = str(intent).strip().lower()
        if key in _INTENT_MODEL:
            return _INTENT_MODEL[key]
    return _DEFAULT


# ── V18 Fase D: recall adaptado al modelo ────────────────────────────────────────
# El volumen de recall debe escalar con la ventana/costo del modelo que lo recibe:
# Opus/Fable (1M) absorben contexto rico; Sonnet quiere recall comprimido; Haiku casi nada.
_TIER_CAPS: dict[str, dict[str, int]] = {
    # semantic = hits semánticos; decisions/guards = filas; trunc = corte de texto por línea.
    # cowork = filas de cowork_comments (commit-anchored feedback); 0 = omit section.
    "full":       {"semantic": 999, "decisions": 999, "guards": 999, "trunc": 0,   "cowork": 5},
    "compact":    {"semantic": 3,   "decisions": 2,   "guards": 3,   "trunc": 200, "cowork": 2},
    "guard_only": {"semantic": 0,   "decisions": 0,   "guards": 3,   "trunc": 120, "cowork": 0},
}


def recall_tier(model: Optional[str] = None) -> str:
    """Tier de recall para el modelo dado (o el hilo de sesión si None).

    Opus/Fable (1M) → 'full'; Sonnet → 'compact'; Haiku → 'guard_only'. Desconocido → 'full'
    (no recortar sin certeza). Materializa: no mandar recall pesado a un verificador acotado.
    """
    m = _short_name(model) if model else session_model()
    if m == "haiku":
        return "guard_only"
    if m == "sonnet":
        return "compact"
    return "full"  # opus, fable, desconocido → sin recorte


def tier_caps(tier: str) -> dict[str, int]:
    """Límites (semantic/decisions/guards/trunc) del tier. Tier inválido → 'full'."""
    return dict(_TIER_CAPS.get(tier, _TIER_CAPS["full"]))


def _short_name(raw: Optional[str]) -> str:
    """Normaliza un nombre de modelo a tier corto ('' si no reconocido)."""
    low = str(raw or "").lower()
    for name in ("fable", "opus", "sonnet", "haiku"):
        if name in low:
            return name
    return ""


def session_model() -> str:
    """Modelo del HILO de sesión (best-effort), para advertir sobre herencia.

    Lee ~/.claude/settings.json (campo 'model') o ARIS4U_CLAUDE_MODEL. Devuelve un
    nombre corto ('fable'/'opus'/'sonnet'/'haiku') o '' si no se puede determinar.
    """
    raw = os.environ.get("ARIS4U_CLAUDE_MODEL", "")
    if not raw:
        try:
            p = Path.home() / ".claude" / "settings.json"
            raw = json.loads(p.read_text(encoding="utf-8")).get("model", "") if p.is_file() else ""
        except Exception:
            raw = ""
    return _short_name(raw)


def routing_hint(intent: Optional[str], novelty_deep: bool = False) -> str:
    """Línea compacta de routing para inyectar en additionalContext (auto-inyección V18).

    Orienta los SUBAGENTES del turno (no el hilo, que ya es su modelo). Advierte del
    peligro de herencia si el hilo es Fable/Opus.

    Args:
        intent: Intención F1 clasificada.
        novelty_deep: True si novelty detectó dominio nuevo (exploración profunda).

    Returns:
        Una línea de texto lista para additionalContext.
    """
    sess = session_model()
    inherit_warn = ""
    if sess in ("fable", "opus"):
        inherit_warn = f" · sesión={sess}: heredar sin model= es CARO"
        if not novelty_deep and (intent or "") in ("implementation", "fix", "simple"):
            inherit_warn += (
                " · ⚠ tarea rutinaria en hilo top — sesiones así ábrelas en sonnet"
                " (model-governance.md H1)"
            )
    dom = route_model(intent="decision" if novelty_deep else (intent or ""))
    return (
        f"🧭 ROUTING: `model=` en cada Agent() — "
        f"síntesis/veredicto→opus · grueso→sonnet · trivial→haiku"
        f"{inherit_warn}. Dominante≈{dom}."
    )


# ── CLI: `python3 tools/model_router.py <subtask> [--intent X]` ───────────────────
def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Router de modelos Claude para subagentes")
    ap.add_argument("subtask", nargs="?", default=None, help="tipo de subtarea (verify/synthesis/…)")
    ap.add_argument("--intent", default=None, help="intención F1 (decision/implementation/…)")
    ap.add_argument("--hint", action="store_true", help="imprime la línea de routing_hint")
    ns = ap.parse_args(argv)
    if ns.hint:
        print(routing_hint(ns.intent))
    else:
        print(route_model(ns.subtask, intent=ns.intent))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
