#!/usr/bin/env python3
"""Enrutador de capacidades — matchea un prompt contra el catálogo de triggers.

Paso 3-4 del Enrutador (architecture/CAPABILITY_ROUTER_PLAN.md §5.3-5.4). Dado el
prompt del usuario (+ intención + cwd), devuelve 1-3 capacidades de ALTA confianza
para sugerir ("para esta tarea tienes X"). El hook UserPromptSubmit lo invoca para
inyectar el hint — volviendo lo opt-in (que se olvida) en auto-sugerido.

Diseño:
  - PRECISIÓN ante todo: un hint equivocado entrena a ignorar los hints. Solo dispara
    si hay trigger Y ningún anti-trigger Y la intención/contexto casan.
  - RÁPIDO: NO escanea el filesystem en cada prompt; confía en ``data/
    capability_triggers.json`` (catálogo curado SOLO de capacidades vivas/ruteables;
    la liveness se valida al regenerar el catálogo, no en caliente).
  - Tope duro de hints (default 2); orden por confianza y nº de triggers casados.

Uso:
    python3 tools/capability_router.py "necesito decidir la arquitectura de X"
    python3 tools/capability_router.py --intent decision --cwd /ruta "..."
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

ARIS_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ARIS_ROOT / "data" / "capability_triggers.json"
_EVENTS_LOG = ARIS_ROOT / "logs" / "v16.1-events.jsonl"

_CONFIDENCE_WEIGHT = {"high": 2, "med": 1, "low": 0}

# Hints verificados como muertos (disparan mucho, adopción ~0) que se suprimen de forma
# estática como respaldo cuando el event log no tiene datos suficientes todavía.
# Mantenimiento: añadir aquí solo tras confirmar en conductor_stats que adopted=0, N>=5.
_STATIC_DEAD_HINTS: frozenset[str] = frozenset({
    # profiles:aris4u: dispara ~63/185 pero auto_recall ya inyecta el contexto
    # ARIS4U en cada sesión → 100% redundante con lo que auto_recall da (0/24).
    "profiles:aris4u",
})

# Mínimo de hints RESUELTOS (adopted+ignored) para suprimir dinámicamente.
_MIN_RESOLVED_FOR_SUPPRESSION = 5

# Skills ARIS4U nucleares protegidas de la supresión automática aunque el log las marque
# con adopción 0/N. Razón: la adopción vía hint es solo UNA de las vías de entrada — estas
# skills se invocan frecuentemente de forma directa (sin hint como trigger), por lo que
# adopted=0 no significa que sean inútiles, sino que el camino hint→uso no es el primario.
# No añadir aquí sin justificación: el propósito es proteger, no blanquear hints muertos.
_PROTECTED_HINTS: frozenset[str] = frozenset({
    "aris-council",   # decisión arquitectónica — invocación directa, hint llega tarde
    "preflight",      # audit recursos — se invoca antes del fan-out, hint puede ser tardío
    "harvest",        # cierre de bloque — se invoca al cerrar, no por hint proactivo
    "second-auditor", # gate final — invocado explícitamente al cerrar, no por hint
})


def load_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    """Carga el catálogo de triggers (lista de entradas). [] si falta/roto."""
    p = path or CATALOG_PATH
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):  # admite {"catalog": [...]} o lista pelada
        data = data.get("catalog") or data.get("entries") or []
    return data if isinstance(data, list) else []


def _intent_ok(entry_intents: list[str], intent: str) -> bool:
    """La intención del prompt casa con la de la entrada (o no descalifica)."""
    if not entry_intents or "any" in entry_intents:
        return True
    if not intent:  # intención desconocida → no descalifica
        return True
    return intent in entry_intents


def _context_ok(ctx: str, cwd: str) -> bool:
    """El contexto requerido se cumple, o no aplica.

    ``ctx`` es uno o varios substrings de cwd separados por '|' (OR). Vacío = no aplica.
    """
    if not ctx:
        return True
    cwd_lc = (cwd or "").lower()
    return any(p.strip() and p.strip() in cwd_lc for p in ctx.lower().split("|"))


def _score(entry: dict[str, Any], prompt_lc: str, intent: str, cwd: str) -> int:
    """Puntúa una entrada contra el prompt. 0 = no sugerir."""
    if any(a and a.lower() in prompt_lc for a in entry.get("anti_triggers", [])):
        return 0
    hits = sum(1 for t in entry.get("triggers", []) if t and t.lower() in prompt_lc)
    if not hits:
        return 0
    if not _intent_ok(entry.get("intent", []), intent):
        return 0
    if not _context_ok(entry.get("context", ""), cwd):
        return 0
    return hits * (_CONFIDENCE_WEIGHT.get(entry.get("confidence", "med"), 1) + 1)


def _keyword_hits(
    cat: list[dict[str, Any]], prompt_lc: str, intent: str, cwd: str
) -> list[dict[str, Any]]:
    """Hits por keyword substring (comportamiento histórico), ordenados por score."""
    scored = [
        (s, e) for e in cat if (s := _score(e, prompt_lc, intent, cwd)) > 0
    ]
    scored.sort(key=lambda x: -x[0])
    return [
        {
            "name": e["name"],
            "hint": e["hint"],
            "confidence": e.get("confidence", "med"),
            "score": s,
            # Un FLUJO de escenario lleva una secuencia ordenada de pasos (qué→herramienta→orden);
            # una capacidad suelta no. None si la entrada no es un flujo.
            "flow": e.get("flow"),
            "via": "keyword",
        }
        for s, e in scored
    ]


def _dead_hints_from_log(log_path: Path | None = None) -> frozenset[str]:
    """Devuelve nombres de capacidad con adopción 0/N (N>=_MIN_RESOLVED_FOR_SUPPRESSION).

    Lee el event log JSONL y calcula, para cada nombre de capacidad, cuántos eventos
    ``capability_adopted`` y ``capability_ignored`` existen. Si un nombre tiene
    ``adopted=0`` y ``adopted+ignored >= _MIN_RESOLVED_FOR_SUPPRESSION``, se considera
    muerto y se suprime. Fail-open total: cualquier error → conjunto vacío (los hints
    siguen apareciendo; nunca rompas el routing por un fallo de lectura).

    Args:
        log_path: Ruta al event log JSONL (por defecto ``logs/v16.1-events.jsonl``).

    Returns:
        Frozenset de nombres de capacidad a suprimir, o vacío si no hay datos suficientes.
    """
    try:
        p = log_path or _EVENTS_LOG
        if not p.exists():
            return frozenset()
        adopted: dict[str, int] = {}
        ignored: dict[str, int] = {}
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = ev.get("event", "")
            name = ev.get("name", "")
            if not name:
                continue
            if event == "capability_adopted":
                adopted[name] = adopted.get(name, 0) + 1
            elif event == "capability_ignored":
                ignored[name] = ignored.get(name, 0) + 1
        dead: set[str] = set()
        all_names = set(adopted) | set(ignored)
        for name in all_names:
            if name in _PROTECTED_HINTS:
                continue  # nunca suprimir skills nucleares por datos de adopción
            a = adopted.get(name, 0)
            total = a + ignored.get(name, 0)
            if a == 0 and total >= _MIN_RESOLVED_FOR_SUPPRESSION:
                dead.add(name)
        return frozenset(dead)
    except Exception:
        return frozenset()


def _suppressed_hints(log_path: Path | None = None) -> frozenset[str]:
    """Une la lista estática y la dinámica del log. Fail-open."""
    dynamic = _dead_hints_from_log(log_path)
    return _STATIC_DEAD_HINTS | dynamic


def _semantic_hits(
    prompt: str, intent: str, cwd: str, exclude_names: set[str]
) -> list[dict[str, Any]]:
    """Hits semánticos (embedding) que NO repiten un keyword hit. [] fail-open total.

    Aislado e import perezoso: si el módulo semántico, numpy, la caché o el embedder no
    están, devuelve ``[]`` y el router cae EXACTO al comportamiento keyword. Nunca lanza.
    """
    try:
        if str(ARIS_ROOT) not in sys.path:
            sys.path.insert(0, str(ARIS_ROOT))
        from tools.capability_semantic import augment

        return augment(prompt, intent=intent, cwd=cwd, exclude_names=exclude_names)
    except Exception:
        return []


def route(
    prompt: str,
    intent: str = "",
    cwd: str = "",
    limit: int = 2,
    catalog: list[dict[str, Any]] | None = None,
    semantic: bool | None = None,
) -> list[dict[str, Any]]:
    """Devuelve capacidades a sugerir para este prompt (keyword ∪ semántico, dedup).

    Combina dos señales: (1) el match por keyword substring del catálogo curado
    (precisión, comportamiento histórico) y (2) el match SEMÁNTICO por embedding contra
    el inventario vivo (cobertura). Los keyword hits van PRIMERO y nunca son desplazados
    por los semánticos (precisión > cobertura); los semánticos solo AUMENTAN (llenan los
    slots restantes/extra). Si un match semántico es de alta confianza, el tope sube de
    ``limit`` (2) a ~4 para que quepan tanto el flujo/keyword como las capacidades extra.

    Args:
        prompt: texto del usuario.
        intent: intención clasificada (decision/research/implementation/fix/simple).
        cwd: directorio de trabajo (para triggers con contexto de proyecto/cliente).
        limit: tope base de hints (precisión > cobertura).
        catalog: catálogo explícito (para tests); si None, se carga del disco.
        semantic: forzar capa semántica on/off. Default ``None`` → solo activa en el
            camino de producción (``catalog is None``); con un catálogo explícito (tests)
            queda OFF para mantener el determinismo y no depender de Ollama.

    Returns:
        Lista de ``{name, hint, confidence, score, flow, via}`` ordenada por relevancia
        (keyword primero, luego semántico).
    """
    prompt_lc = (prompt or "").lower()
    cat = catalog if catalog is not None else load_catalog()
    keyword_hits = _keyword_hits(cat, prompt_lc, intent, cwd)

    use_semantic = (catalog is None) if semantic is None else semantic
    eff_limit = limit
    sem_hits: list[dict[str, Any]] = []
    if use_semantic:
        kw_names = {h["name"] for h in keyword_hits}
        sem_hits = _semantic_hits(prompt or "", intent, cwd, kw_names)
        if sem_hits:
            try:
                from tools.capability_semantic import high_conf_threshold

                if sem_hits[0].get("sim", 0.0) >= high_conf_threshold():
                    eff_limit = max(limit, 4)
            except Exception:
                pass

    # Suprimir hints con adopción 0/N (dinámica desde el log + lista estática de muertos).
    # Solo en producción (catalog is None); con catálogo sintético de tests = siempre off.
    dead: frozenset[str] = frozenset()
    if catalog is None:
        dead = _suppressed_hints()

    all_hits = [h for h in (keyword_hits + sem_hits) if h["name"] not in dead]
    return all_hits[:eff_limit]


def _autopilot_on() -> bool:
    """True si el modo autopilot está activo (``ARIS4U_AUTOPILOT``). Off por defecto.

    En autopilot los hints se formulan de forma IMPERATIVA ("ejecuta ahora") en vez de
    sugerente ("considéralas"), para el usuario —típicamente no-desarrollador— que quiere
    "hablo y se ejecuta" sin tener que elegir. Sigue siendo advisory: un hook no puede
    forzar un tool-call, solo sube el registro del texto que Claude lee. Fail-safe: cualquier
    valor no reconocido → off.

    Returns:
        True si ``ARIS4U_AUTOPILOT`` está en 1/true/yes/on; False en cualquier otro caso.
    """
    return os.environ.get("ARIS4U_AUTOPILOT", "0").strip().lower() in ("1", "true", "yes", "on")


def format_hints(hints: list[dict[str, Any]]) -> str:
    """Formatea los hints para inyectar como additionalContext (vacío si no hay).

    Distingue dos tipos: un FLUJO de escenario (entrada con ``flow``: lista de pasos
    ordenados) se renderiza como secuencia numerada y prominente; una capacidad suelta se
    lista como bullet. El flujo va primero porque define el ORDEN de la tarea.

    Con ``ARIS4U_AUTOPILOT`` activo, los encabezados pasan de sugerentes a imperativos
    (ejecuta ahora, no le pidas al usuario que elija) — mismo contenido de hints, mayor
    registro. Con autopilot off el texto es idéntico al histórico (tests intactos).
    """
    if not hints:
        return ""
    autopilot = _autopilot_on()
    flows = [h for h in hints if h.get("flow")]
    singles = [h for h in hints if not h.get("flow")]
    lines: list[str] = []
    for f in flows:
        if autopilot:
            lines.append(
                f"🎯 AUTOPILOT — la petición corresponde a un flujo conocido ({f['hint']}). "
                "EJECÚTALO ahora de principio a fin; no le pidas al usuario que elija los pasos:"
            )
        else:
            lines.append(f"🔀 Flujo recomendado para esta tarea — {f['hint']}:")
        for i, step in enumerate(f["flow"], 1):
            lines.append(f"  {i}. {step}")
        if autopilot:
            lines.append("  (el usuario se enfoca en el QUÉ; tú ejecutas el CÓMO por él)")
        else:
            lines.append("  (define el QUÉ y el ORDEN; ajusta si el caso lo pide)")
    if singles:
        if autopilot:
            lines.append("🎯 AUTOPILOT — usa estas capacidades de ARIS4U AHORA (no solo las menciones):")
        else:
            lines.append("💡 Capacidades de ARIS4U para esta tarea (opt-in, considéralas):")
        lines += [f"  · {h['hint']}" for h in singles]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """CLI: enruta el prompt dado y muestra los hints."""
    intent, cwd, words = "", "", []
    i = 0
    while i < len(argv):
        if argv[i] == "--intent" and i + 1 < len(argv):
            intent = argv[i + 1]
            i += 2
        elif argv[i] == "--cwd" and i + 1 < len(argv):
            cwd = argv[i + 1]
            i += 2
        else:
            words.append(argv[i])
            i += 1
    hints = route(" ".join(words), intent=intent, cwd=cwd)
    if "--json" in argv:
        print(json.dumps(hints, indent=2, ensure_ascii=False))
    else:
        print(format_hints(hints) or "(sin hints para este prompt)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
