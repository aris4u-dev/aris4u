#!/usr/bin/env python3
"""Protocolo de orquestación — reencuadra a Claude como orquestador de su toolkit.

Fase 3 del enrutador de capacidades. El techo de Claude Code es claro: un hook NO
puede invocar una skill ni forzar un tool-call; solo puede INYECTAR texto que el
modelo lee. Esta capa produce ese texto: un bloque imperativo, compacto y graduado
que recuerda a Claude recorrer el ciclo de trabajo (ENTENDER → DISEÑAR → CONSTRUIR →
VERIFICAR) usando la capacidad CORRECTA de su inventario VIVO en cada paso, sin que
el usuario tenga que invocar nada.

Diseño (mismas leyes que el router):
  - GENÉRICO: los nombres candidatos por fase son capacidades de PRIMERA PARTE
    (ARIS4U / Claude nativo), NUNCA nombres de cliente. Se FILTRAN contra el
    inventario vivo del usuario: solo se nombra lo que esa instancia realmente tiene;
    si una fase no tiene ninguna capacidad presente, usa guía genérica ("usa la
    capacidad de tu inventario para <fase>"). Funciona para cualquier tercero.
  - GRADUADO por intención del clasificador F1: ``simple`` (o desconocida) → NADA (no
    molestar en trivial); ``fix`` → versión ligera; ``decision``/``research``/
    ``implementation`` → ciclo completo ordenado.
  - FAIL-OPEN: si el inventario no se puede leer (snapshot ausente/corrupto) →
    ``""``. Mejor NO inyectar que inyectar nombres que el usuario no tiene o romper
    el hook. Nunca lanza en el camino de producción (los callers además lo envuelven).
  - COMPACTO: presupuesto duro de chars (el bloque no debe inflar el contexto).

El inventario VIVO se lee de ``data/capability_runtime_snapshot.json`` (regenerado
por SessionStart en background). Lectura barata (un JSON), apta para el hot path.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ARIS_ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = ARIS_ROOT / "data" / "capability_runtime_snapshot.json"

# Presupuesto duro del bloque per-turn (chars). Si lo supera, se recorta a la versión
# ligera (anti-bloat: el protocolo NUNCA debe dominar el additionalContext).
MAX_PROTOCOL_CHARS = 900

# Intenciones del clasificador F1 que reciben el ciclo completo vs. la versión ligera.
_FULL_INTENTS = frozenset({"decision", "research", "implementation"})
_LIGHT_INTENTS = frozenset({"fix"})

# Ciclo de orquestación. Cada fase: (etiqueta, qué-hace, candidatos ordenados por
# preferencia). Los candidatos son capacidades de PRIMERA PARTE (no clientes); se
# filtran contra el inventario vivo, así que un tercero con otro toolkit ve solo lo
# que tiene (o la guía genérica). El orden de candidatos = preferencia de uso.
_PHASES: list[tuple[str, str, list[str]]] = [
    (
        "ENTENDER",
        "recupera contexto y aclara el problema",
        ["aris_recall_client", "aris_search", "clarify", "discover",
         "feature-dev:code-explorer"],
    ),
    (
        "DISEÑAR",
        "decide enfoque/arquitectura antes de teclear",
        ["aris-council", "feature-dev:code-architect"],
    ),
    (
        "CONSTRUIR",
        "ejecuta con el especialista, no a mano genérico",
        ["feature-dev:feature-dev", "enterprise-build", "feature-dev"],
    ),
    (
        "VERIFICAR",
        "gate de cierre antes de declarar 'listo'",
        ["second-auditor", "code-review", "verify-claims", "aris_dialectic"],
    ),
]

# Candidatos de la fase de verificación (para la versión ligera de ``fix``).
_VERIFY_CANDIDATES = _PHASES[-1][2]


def _read_snapshot(snapshot_path: Path | None) -> dict[str, Any] | None:
    """Lee y parsea el snapshot del inventario vivo. None ante I/O/JSON inválido."""
    p = snapshot_path or SNAPSHOT_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _names_from_unified(data: dict[str, Any]) -> set[str]:
    """Nombres del formato unificado collect(): ``{"capabilities": [{"name": ...}]}``."""
    caps = data.get("capabilities")
    if not isinstance(caps, list):
        return set()
    return {str(c["name"]) for c in caps if isinstance(c, dict) and c.get("name")}


def _names_from_lists(data: dict[str, Any]) -> set[str]:
    """Nombres de las listas planas por tipo del snapshot vivo."""
    names: set[str] = set()
    for key in ("skills", "agents", "commands", "builtin_tools"):
        v = data.get(key)
        if isinstance(v, list):
            names.update(str(x) for x in v if x)
    return names


def _names_from_mcp_tools(data: dict[str, Any]) -> set[str]:
    """Nombres de ``mcp_tools`` (dict server->[tools] del snapshot vivo, o lista plana)."""
    mt = data.get("mcp_tools")
    names: set[str] = set()
    if isinstance(mt, dict):
        for tools in mt.values():
            if isinstance(tools, list):
                names.update(str(x) for x in tools if x)
    elif isinstance(mt, list):
        names.update(str(x) for x in mt if x)
    return names


def available_capability_names(
    snapshot: dict[str, Any] | None = None,
    snapshot_path: Path | None = None,
) -> set[str]:
    """Conjunto de nombres de capacidad del inventario vivo (fail-open ``set()``).

    Acepta tanto el formato del snapshot vivo (listas por tipo + ``mcp_tools`` dict)
    como el formato unificado de ``capability_inventory.collect()`` (lista
    ``capabilities`` de dicts con ``name``). Lectura barata; cualquier fallo de
    I/O o JSON degrada a ``set()`` (→ el protocolo no se inyecta).

    Args:
        snapshot: Snapshot ya parseado (para tests); si None, se lee de disco.
        snapshot_path: Ruta alterna del snapshot; por defecto ``SNAPSHOT_PATH``.

    Returns:
        Conjunto de nombres de capacidad presentes, o ``set()`` si no se puede leer.
    """
    data = snapshot if isinstance(snapshot, dict) else _read_snapshot(snapshot_path)
    if data is None:
        return set()
    return _names_from_unified(data) | _names_from_lists(data) | _names_from_mcp_tools(data)


def _phase_caps(candidates: list[str], names: set[str], max_show: int = 2) -> list[str]:
    """Candidatos de una fase que SÍ están en el inventario vivo (máx ``max_show``)."""
    return [c for c in candidates if c in names][:max_show]


def _build_full(intent: str, names: set[str]) -> str:
    """Ciclo completo ordenado (decision/research/implementation)."""
    # Descriptors compactos por fase: misma señal, menos tokens
    _PHASE_DESC_SHORT = {
        "ENTENDER": "contexto/clarify",
        "DISEÑAR":  "enfoque/arch",
        "CONSTRUIR": "especialista",
        "VERIFICAR": "gate cierre",
    }
    lines = [
        f"🧭 ORQUESTA (intent={intent}) — ciclo por capacidad del inventario:"
    ]
    for i, (label, _desc, cands) in enumerate(_PHASES, 1):
        present = _phase_caps(cands, names)
        if present:
            tail = " · ".join(present)
            if label == "CONSTRUIR":
                tail += " · especialista de tu roster"
        else:
            tail = f"cap de inventario para {label.lower()}"
        if label == "VERIFICAR":
            native = "tests+lint/types"
            tail = f"{native} · {tail}" if present else native
        desc_short = _PHASE_DESC_SHORT.get(label, _desc)
        lines.append(f"  {i}. {label} ({desc_short}) → {tail}")
    lines.append(
        "  (TU inventario vivo; equivalente si falta — no improvises lo que una cap ya cubre)"
    )
    return "\n".join(lines)


def _build_light(names: set[str]) -> str:
    """Versión ligera (fix): ciclo corto en una línea, sin desplegar las 4 fases.

    VERIFICAR nombra SIEMPRE los gates nativos (tests + lint/types), que todo developer
    tiene de fábrica, y añade el gate de cierre del inventario si está instalado.
    """
    verify = _phase_caps(_VERIFY_CANDIDATES, names, 1)
    v = f"tests+lint/types o {verify[0]}" if verify else "tests+lint/types"
    return (
        "🧭 ORQUESTA (fix): ENTENDER (recall previo) → CONSTRUIR (fix puntual) → "
        f"VERIFICAR ({v})."
    )


def build_protocol(intent: str, names: set[str] | None = None) -> str:
    """Bloque de protocolo per-turn para inyectar como additionalContext.

    Graduado por intención (clasificador F1) y filtrado contra el inventario vivo.
    Fail-open: devuelve ``""`` para intención trivial/desconocida o si el inventario
    no está disponible (mejor nada que inyectar nombres ausentes o romper el hook).

    Args:
        intent: Intención clasificada ('simple'/'fix'/'decision'/'implementation'/
            'research'). 'simple' o desconocida → ``""`` (no molestar en trivial).
        names: Conjunto de nombres del inventario vivo; si None se lee del snapshot.

    Returns:
        El bloque de protocolo (graduado), o ``""`` si no aplica/falla.
    """
    if intent not in _FULL_INTENTS and intent not in _LIGHT_INTENTS:
        return ""  # simple / desconocida → no inyectar
    if names is None:
        names = available_capability_names()
    if not names:
        return ""  # inventario no disponible → fail-open neutral (mejor nada)

    if intent in _LIGHT_INTENTS:
        block = _build_light(names)
    else:
        block = _build_full(intent, names)
        # Anti-bloat: si por alguna razón el ciclo completo se infla, cae a la ligera.
        if len(block) > MAX_PROTOCOL_CHARS:
            block = _build_light(names)
    return block


def build_session_posture(names: set[str] | None = None) -> str:
    """Postura de orquestación para SessionStart (una vez por sesión, compacta).

    Instala la actitud de orquestador genérica: Claude opera su propio toolkit y
    recorre el ciclo en cada tarea no-trivial, usando la capacidad correcta de su
    inventario vivo. El detalle por-tarea lo inyecta el protocolo per-turn.

    Args:
        names: Inventario vivo; si None se lee del snapshot. Se usa solo para
            decidir si hay toolkit que orquestar (si está vacío → ``""``).

    Returns:
        Bloque corto de postura, o ``""`` si no hay inventario legible (fail-open).
    """
    if names is None:
        names = available_capability_names()
    if not names:
        return ""
    return (
        "🧭 POSTURA — no improvises lo que una cap ya cubre.\n"
        "  Ciclo: ENTENDER→DISEÑAR→CONSTRUIR→VERIFICAR con TU inventario vivo. "
        "Detalle por-tarea en cada prompt."
    )


def main(argv: list[str]) -> int:
    """CLI de prueba: imprime el protocolo per-turn para una intención dada."""
    intent = argv[0] if argv else "implementation"
    names = available_capability_names()
    print(f"# inventario vivo: {len(names)} capacidades")
    print("# --- postura de sesión ---")
    print(build_session_posture(names) or "(sin inventario → sin postura)")
    print(f"# --- protocolo per-turn (intent={intent}) ---")
    print(build_protocol(intent, names) or "(sin protocolo para esta intención)")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
