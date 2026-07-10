"""Handler UserPromptSubmit — portado de hooks/depth_inject.sh (sin heredoc shell).

El hook más caliente de ARIS4U: corre en CADA prompt y es la puerta del Depth
Protocol + la memoria viva. Porta EXACTO la lógica del .sh (compact v16.10):

  1. Detección de cliente desde cwd + puente para el demonio MCP (write_client_bridge.sh).
  2. Clasificación de intención/profundidad vía v16_orchestrator (F1→F6) + novelty
     override (nuevo dominio → exploración profunda 1-10).
  3. 🧭 MODEL_HINT advisory (commit 92d9d68): Opus 4.8 / Sonnet / Haiku según
     query_type — texto puramente advisory, nunca fuerza modelo. Telemetría model_hint.
  4. WAVE timer (>80m), TOKEN budget (≥umbral), EFFORT (si != medium).
  5. DEPTH directive + decisiones LOCKED (scoped por cliente + globales).
  6. 🧠 RECALL híbrido (FTS5 + semántico) con cap duro SIGALRM 2s + fail-open.
     Telemetría auto_recall.
  7. Persistencia del estado de sesión (/tmp/aris4u_session_state.json) + GOAL tracking.
  8. Telemetría depth_inject si ARIS4U_VALIDATION_LOG.

DIFERENCIA LEGÍTIMA vs el .sh (contrato del dispatcher, no cambio de comportamiento):
  - El prompt llega en el payload JSON del evento (`inp["prompt"]`), no por `cat` de
    stdin crudo. Fallback a raw si algún campo trae el texto suelto.
  - El cwd se toma del evento (`inp["cwd"]`), espejando session_start, en vez de `pwd`
    (el dispatcher puede correr con cwd neutro). La detección de cliente es idéntica.

Salida: additionalContext (mismo formato legacy que emite el .sh con print + exit 0).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, UTC
from pathlib import Path
from typing import TYPE_CHECKING

from dispatch.contract import ARIS4U_ROOT, emit_additional_context, passthrough

if TYPE_CHECKING:  # solo para tipado; no carga el engine en el hot path (annotations diferidas)
    from engine.v16.v16_orchestrator import V16QueryResult

STATE_FILE = Path("/tmp/aris4u_session_state.json")

# Sufijos de carpeta que se quitan al canonizar el nombre de cliente
# (cliente-platform → cliente). NUNCA partir en el primer guión.
_CLIENT_SUFFIXES = ("-platform", "-website", "-app", "-web")


def _detect_client(cwd: str) -> str:
    """Cliente desde cwd: ~/projects/03-clients/{client}/... → lower-case sin sufijo.

    Espeja exactamente el bloque bash de depth_inject.sh (y
    session_manager.resolve_client_from_path): match del componente tras
    /projects/03-clients/, lower-case, quitar sufijo conocido. '' si no aplica.
    """
    parts = Path(cwd).parts
    try:
        i = parts.index("03-clients")
    except ValueError:
        return ""
    if i + 1 >= len(parts):
        return ""
    client = parts[i + 1].lower()
    for suf in _CLIENT_SUFFIXES:
        if client.endswith(suf):
            client = client[: -len(suf)]
    return client


def _log_event(event: dict) -> None:
    """Append best-effort al event log (telemetría model_hint / auto_recall).

    Ruta: ``$ARIS4U_EVENTS_LOG`` si está seteada (los tests la apuntan a un tmp
    para no contaminar el log de producción), si no ``logs/v16.1-events.jsonl``.
    """
    try:
        override = os.environ.get("ARIS4U_EVENTS_LOG")
        lf = Path(override) if override else ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"
        if lf.parent.exists():
            with lf.open("a") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass


def _setup_client(cwd: str) -> str:
    """Detecta el cliente del cwd, escribe el puente MCP y exporta ARIS4U_CLIENT.

    Espeja el preámbulo del .sh: detección de cliente (03-clients), invocación de
    ``write_client_bridge.sh`` (fail-open, el demonio MCP no ve el cwd) y export del
    cliente activo para que session_manager/telemetría lo resuelvan.

    Args:
        cwd: Directorio de trabajo del evento.

    Returns:
        El nombre canónico del cliente, o '' si el cwd no está bajo 03-clients.
    """
    client = _detect_client(cwd)
    try:
        subprocess.run(
            ["bash", str(ARIS4U_ROOT / "hooks" / "write_client_bridge.sh"), cwd],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass
    if str(ARIS4U_ROOT) not in sys.path:
        sys.path.insert(0, str(ARIS4U_ROOT))
    if client:
        os.environ["ARIS4U_CLIENT"] = client
    return client


def _classify_levels(
    query: str, v16_result: V16QueryResult
) -> tuple[str, str | None, dict | None, str]:
    """Resuelve query_type, override de novelty, rationale adaptativo y el string de niveles.

    Porta EXACTO la cascada del .sh: novelty override (nuevo dominio → exploración
    profunda 1-10, query_type='implementation') con prioridad; si no, los niveles/estrategia
    del v16_result (fail-open a get_levels). Cada bloque conserva su try/except.

    Args:
        query: El prompt recortado a 500 chars.
        v16_result: Resultado del orquestador V16 (intent/depth_levels/strategy/confidence).

    Returns:
        Tupla ``(query_type, novelty_override, adaptive_rationale, level_names)``.
    """
    from engine.v16.depth_protocol import LEVEL_NAMES, get_levels

    query_type = v16_result.intent
    novelty_override: str | None = None
    adaptive_rationale: dict | None = None
    # Inicializado por defecto: ambas ramas de abajo lo asignan (novelty → 1-10; si no →
    # depth_levels/get_levels), pero pyright no puede correlacionar novelty_override con
    # levels. El '[]' nunca persiste (es solo el piso del análisis de flujo).
    levels: list[int] = []
    try:
        from engine.v16.novelty_detector import detect_novelty

        novelty_result = detect_novelty(query)
        if novelty_result.is_new_domain:
            novelty_override = "deep_exploration"
            levels = list(range(1, 11))
            adaptive_rationale = {"reason": "V16_novelty_deep_exploration", "confidence": 0.95}
            query_type = "implementation"
    except Exception:
        pass

    if novelty_override != "deep_exploration":
        try:
            levels = v16_result.depth_levels
            strategy_str = f"V16_F2_{v16_result.strategy}"
            adaptive_rationale = {"reason": strategy_str, "confidence": v16_result.confidence}
        except Exception:
            levels = get_levels(query_type)
            adaptive_rationale = None

    level_names = ", ".join(LEVEL_NAMES[lvl] for lvl in levels)
    return query_type, novelty_override, adaptive_rationale, level_names


def _append_model_hint(
    parts: list[str],
    query_type: str,
    novelty_override: str | None,
    v16_result: V16QueryResult,
    depth_on: bool,
) -> None:
    """MODEL ROUTING advisory: sugiere modelo por intención + telemetría model_hint.

    Texto PURAMENTE advisory (nunca fuerza modelo) y sólo se inyecta a ``parts`` si
    ``depth_on``; la telemetría se loguea siempre. Fail-open: cualquier excepción se traga.

    Args:
        parts: Acumulador de líneas de additionalContext (mutado in-place).
        query_type: Intención clasificada.
        novelty_override: 'deep_exploration' si novelty detectó nuevo dominio.
        v16_result: Resultado V16 (para la confianza loguead).
        depth_on: Si el Depth Protocol está activo (flag ARIS4U_DEPTH_PROTOCOL).
    """
    try:
        _conf = 0.0
        try:
            _conf = float(getattr(v16_result, "confidence", 0.0) or 0.0)
        except Exception:
            _conf = 0.0
        # V18 Fase A: el hint orienta los SUBAGENTES del fan-out (no el hilo, que ya corre
        # su modelo — hoy Fable). Motor de decisión = tools/model_router. Se emite SIEMPRE
        # (auto-inyección, decisión del usuario 2026-07-02): el routing NO depende de depth_on, a
        # diferencia de la directiva de profundidad. Fallback local si el módulo no carga.
        _novelty_deep = novelty_override == "deep_exploration"
        _ = depth_on  # el routing ya no se gatea por DEPTH_PROTOCOL (siempre visible)
        try:
            if str(ARIS4U_ROOT) not in sys.path:
                sys.path.insert(0, str(ARIS4U_ROOT))
            from tools.model_router import route_model, routing_hint

            _hint_line = routing_hint(query_type, novelty_deep=_novelty_deep)
            _dom_model = route_model(intent="decision" if _novelty_deep else query_type)
        except Exception:
            _dom_model = (
                "opus"
                if (_novelty_deep or query_type in ("decision", "research"))
                else "haiku" if query_type == "simple" else "sonnet"
            )
            _hint_line = (
                "🧭 ROUTING: `model=` en cada Agent() — "
                "síntesis→opus · grueso→sonnet · trivial→haiku."
            )
        parts.append(_hint_line)

        _log_event(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "model_hint",
                "intent": query_type,
                "model": _dom_model,
                "confidence": round(_conf, 3),
                "novelty": bool(_novelty_deep),
                "client": os.environ.get("ARIS4U_CLIENT", ""),
            }
        )
    except Exception:
        pass


def _load_state() -> dict:
    """Lee el estado de sesión de STATE_FILE. Fail-open a {} si falta o es JSON inválido."""
    state: dict = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            state = {}
    return state


def _append_wave_timer(parts: list[str], state: dict) -> None:
    """WAVE TIMER: inicializa wave_start_time y avisa si la ola lleva >80min.

    Args:
        parts: Acumulador de líneas (mutado in-place).
        state: Estado de sesión (mutado: setea wave_start_time si falta).
    """
    from engine.v16.config import WAVE_DURATION_MINUTES

    if "wave_start_time" not in state:
        state["wave_start_time"] = datetime.now(UTC).isoformat()
    try:
        wave_start = datetime.fromisoformat(state["wave_start_time"])
        elapsed_minutes = (datetime.now(UTC) - wave_start).total_seconds() / 60
        if elapsed_minutes > 80:
            parts.append(f"⏱️ WAVE: {elapsed_minutes:.0f}m/{WAVE_DURATION_MINUTES}m — wrap up soon")
    except Exception:
        pass


def _append_token_and_effort(
    parts: list[str], state: dict, query: str, query_type: str, depth_on: bool
) -> dict:
    """TOKEN BUDGET + EFFORT con una sola instancia de TokenIntelligence (dedup WS-C).

    Inyecta la línea TOKEN si el budget supera el umbral y EFFORT si != 'medium' (sólo
    bajo depth_on). Fail-open: si TokenIntelligence revienta, estima tokens crudo sobre
    el estado entrante.

    Args:
        parts: Acumulador de líneas (mutado in-place).
        state: Estado de sesión (usado como fallback si TI falla).
        query: Prompt recortado.
        query_type: Intención clasificada.
        depth_on: Si el Depth Protocol está activo.

    Returns:
        El estado a usar de aquí en adelante (``ti.state`` en el happy path, o el
        ``state`` entrante mutado con la estimación cruda en el fail-open).
    """
    from engine.v16.config import (
        TOKEN_BUDGET_MAX_TOKENS,
        TOKEN_ESTIMATE_RATIO,
        TOKEN_WARN_THRESHOLD_PCT,
    )

    try:
        from engine.v16.token_utils import TokenIntelligence

        ti = TokenIntelligence()
        ti.log_query(query, query_type)
        _budget_pct = ti.get_budget_pct()
        if _budget_pct >= TOKEN_WARN_THRESHOLD_PCT:
            parts.append(
                f'TOKEN: {ti.state.get("accumulated_token_estimate", 0) // 1000}k/{TOKEN_BUDGET_MAX_TOKENS // 1000}k'
            )
        state = ti.state
        _effort = ti.get_effort_level(query_type)
        if depth_on and _effort != "medium":
            parts.append(f"EFFORT: {_effort.upper()}")
    except Exception:
        state.setdefault("accumulated_token_estimate", 0)
        state["accumulated_token_estimate"] += len(query) // TOKEN_ESTIMATE_RATIO
    return state


def _append_depth_directive(
    parts: list[str], query_type: str, level_names: str, adaptive_rationale: dict | None
) -> None:
    """CORE DEPTH DIRECTIVE: 'DEPTH: simple' o 'DEPTH: <intent> | <niveles>' + Adaptive.

    Sólo se llama bajo depth_on (en sombra Claude usa su Adaptive Thinking nativo).

    Args:
        parts: Acumulador de líneas (mutado in-place).
        query_type: Intención clasificada.
        level_names: Niveles de profundidad ya formateados (coma-separados).
        adaptive_rationale: Rationale de la estrategia F2 (None = sin línea Adaptive).
    """
    if query_type == "simple":
        parts.append("DEPTH: simple")
    else:
        parts.append(f"DEPTH: {query_type} | {level_names}")
        if adaptive_rationale and adaptive_rationale.get("reason") != "default_levels":
            parts.append(f'  [Adaptive: {adaptive_rationale["reason"]}]')


def _append_client_decisions(parts: list[str]) -> None:
    """WS4: decisiones LOCKED scoped por cliente (SELECT directo, cap 3). Fail-open silencioso.

    Args:
        parts: Acumulador de líneas (mutado in-place).
    """
    client_name = os.environ.get("ARIS4U_CLIENT", "")
    if not client_name:
        return
    try:
        from engine.v16 import session_manager

        db = session_manager._connect()
        sql = (
            "SELECT decision, domain, session_ref FROM decisions "
            "WHERE client_id = ? AND locked = 1 ORDER BY created_at DESC LIMIT 3"
        )
        rows = db.execute(sql, [client_name]).fetchall()
        db.close()
        if rows:
            parts.append("")
            parts.append(f"[CLIENTE: {client_name}] Decisiones previas:")
            for decision, domain, ref in rows:
                domain_label = f"({domain})" if domain else "(general)"
                ref_label = f"[{ref}]" if ref else "[unref]"
                parts.append(f"  {ref_label} {domain_label}: {decision[:90]}")
    except Exception as e:
        # No romper el hook, pero NO en silencio: si la memoria por-cliente no se inyecta
        # (DB locked, schema drift) debe quedar traza para detectarlo (audit 2026-06-24).
        _log_event(
            {
                "event": "mem_inject_failed",
                "component": "client_locked",
                "error": f"{type(e).__name__}: {str(e)[:160]}",
            }
        )


def _append_global_locked(parts: list[str], v16_result: V16QueryResult) -> None:
    """Decisiones LOCKED globales del v16_result (cap 2). Fail-open silencioso.

    Args:
        parts: Acumulador de líneas (mutado in-place).
        v16_result: Resultado V16 con ``locked_decisions``.
    """
    try:
        locked = v16_result.locked_decisions
        if locked:
            parts.append("")
            for d in locked[:2]:  # máx 2 para mantener compacto
                ref = d.get("session_ref", "")
                parts.append(f'LOCKED [{ref}]: {d["decision"][:100]}')
    except Exception:
        pass


def _is_atom(_s: dict) -> bool:
    """Un hit es átomo de método si es ``mem_type='fact'`` con señal de patrón reutilizable.

    Señal = ``problem_class`` (eje 1, 75 átomos) O ``structural_signature``
    ('problem_class|artifact_type|regime', 146 átomos). La firma amplía la cobertura a los
    átomos OPERACIONALES (surge/idempotency/FSM/rate-limit) que NO llevan
    problem_class y, sin esto, nunca se levantarían en el boost de recall.
    """
    return _s.get("mem_type") == "fact" and bool(
        _s.get("problem_class") or _s.get("structural_signature")
    )


def _atom_label(_s: dict) -> str:
    """Etiqueta corta del átomo: ``problem_class``, o el 1er eje significativo de
    ``structural_signature`` ('problem_class|artifact_type|regime').

    Para átomos artifact-only el problem_class va como '-' en la firma; saltamos los
    segmentos vacíos/'-' para mostrar el artifact_type (más informativo que '-').
    """
    pc = _s.get("problem_class")
    if pc:
        return str(pc)
    for seg in str(_s.get("structural_signature") or "").split("|"):
        seg = seg.strip()
        if seg and seg != "-":
            return seg
    return "patrón"


def _fmt_semantic_hit(_s: dict) -> list[str]:
    """Formatea un hit semántico para el bloque RECALL.

    Un átomo de método (ver ``_is_atom``) añade una segunda línea PISO-no-techo: muestra
    su dominio de validez y obliga a verificar el régimen antes de aplicar el esqueleto.
    Sin esto el recall estrecha el razonamiento (medido en el test FCRA 2026-06-22).

    Args:
        _s: Hit de semantic_recall (source, source_id, similarity, text, mem_type,
            problem_class, validity_domain, structural_signature).

    Returns:
        Una línea base, más la línea reflexiva si el hit es un átomo de método.
    """
    lines = [
        f"  ~{_s.get('similarity', 0):.2f} [{_s.get('source', '')}#{_s.get('source_id', '')}] "
        f"{str(_s.get('text', ''))[:120]}"
    ]
    if _is_atom(_s):
        _vd = str(_s.get("validity_domain") or "no especificado")[:100]
        lines.append(
            f"     ↳ átomo[{_atom_label(_s)}] válido en: {_vd} — PISO no techo: "
            f"¿qué de ESTE problema NO captura el esqueleto? verifica que el régimen "
            f"aplica antes de usarlo."
        )
    return lines


def _skeleton_lines(_s: dict, query_type: str) -> list[str]:
    """Líneas de PLANTILLA (skeleton) para un átomo, si aplica al build flow.

    Cablea el catálogo al build flow: en intención de construir/arreglar, si el átomo es MUY
    relevante (piso propio) y tiene skeleton, se inyecta su plantilla de código (recortada)
    para que la implementación probada esté a la mano. Devuelve [] si no aplica (otra intención,
    similitud baja, o sin skeleton). Fail-open: cualquier error → sin plantilla.

    Args:
        _s: Hit de átomo (source, source_id, similarity).
        query_type: Intención clasificada (solo implementation/fix inyectan).

    Returns:
        Líneas de la plantilla (cabecera + skeleton recortado), o [] si no aplica.
    """
    from engine.v16.config import (
        SKELETON_INJECT_INTENTS,
        SKELETON_INJECT_MIN_SIM,
        SKELETON_MAX_LINES,
    )

    if query_type not in SKELETON_INJECT_INTENTS:
        return []
    if _s.get("similarity", 0) < SKELETON_INJECT_MIN_SIM:
        return []
    try:
        from engine.v16 import session_manager as _sm

        skel = _sm.get_skeleton(_s.get("source_id"))
    except Exception:
        skel = None
    if not skel:
        return []
    out = ["     📐 PLANTILLA (adapta a TU régimen, no copies a ciegas):"]
    for _ln in str(skel).splitlines()[:SKELETON_MAX_LINES]:
        out.append(f"        {_ln}")
    if len(str(skel).splitlines()) > SKELETON_MAX_LINES:
        out.append("        … (skeleton recortado — ver consola 📐 Plantillas)")
    return out


def _pick_skeleton_atom(atoms: list[dict]) -> int:
    """Índice del átomo cuya PLANTILLA conviene inyectar al construir.

    Prefiere un patrón de SOFTWARE (artifact_type presente en la firma 'pc|at|regime') sobre un
    átomo puro-matemático (solo problem_class): al escribir código, el patrón de software suele
    ser más aplicable que el modelo del mundo. Default: el átomo #1 (mayor similitud). Corrige el
    sesgo medido (idempotencia→probability-estimation) sin perder el orden de relevancia.
    """
    for _i, _s in enumerate(atoms):
        _parts = str(_s.get("structural_signature") or "").split("|")
        _artifact = _parts[1].strip() if len(_parts) > 1 else ""
        if _artifact and _artifact != "-":
            return _i
    return 0


def _build_recall_lines(_res: dict, query_type: str = "") -> tuple[list[str], int, int, int]:
    """Arma las líneas del bloque RECALL a partir del resultado de ``search``.

    Aplica el boost #4: separa el pool semántico en su canal de ÁTOMOS (slots reservados +
    piso de similitud propio) y la memoria general, luego añade decisiones y guards. Split
    de un solo embed → sin coste de latencia. Además cablea el catálogo al build flow: al átomo
    #1, en intención de construir, le inyecta su PLANTILLA (skeleton) si es muy relevante.

    Args:
        _res: Dict de ``session_manager.search`` (semantic/decisions/guards).
        query_type: Intención clasificada (gobierna la inyección de skeleton).

    Returns:
        Tupla ``(líneas, n_semantic, n_atoms, n_skeletons)``.
    """
    from engine.v16.config import ATOM_RECALL_LIMIT, ATOM_RECALL_MIN_SIM

    _semantic = _res.get("semantic", [])
    _atoms: list[dict] = []
    _general: list[dict] = []
    for _s in _semantic:
        if _is_atom(_s) and _s.get("similarity", 0) >= ATOM_RECALL_MIN_SIM:
            _atoms.append(_s)
        else:
            _general.append(_s)

    _rl: list[str] = []
    _n_skel = 0
    _top_atoms = _atoms[:ATOM_RECALL_LIMIT]
    if _top_atoms:
        _rl.append("🧬 ÁTOMOS aplicables (patrón reutilizable — verifica el régimen):")
        _skel_idx = _pick_skeleton_atom(_top_atoms)  # prefiere patrón de software para construir
        for _i, _s in enumerate(_top_atoms):
            _rl.extend(_fmt_semantic_hit(_s))
            if _i == _skel_idx:  # un solo skeleton (anti-bloat), el más aplicable al build
                _sk = _skeleton_lines(_s, query_type)
                if _sk:
                    _rl.extend(_sk)
                    _n_skel = 1
    for _s in _general[:3]:
        _rl.extend(_fmt_semantic_hit(_s))
    for _d in _res.get("decisions", [])[:2]:
        _rl.append(f"  · ({_d.get('domain', '-')}) {str(_d.get('decision', ''))[:120]}")
    for _g in _res.get("guards", [])[:1]:
        _rl.append(f"  ! {str(_g.get('pattern', ''))[:70]} -> {str(_g.get('prevention', ''))[:70]}")
    return _rl, len(_semantic), len(_top_atoms), _n_skel


def _tier_recall_result(res: dict) -> dict:
    """Recorta el resultado de search al tier de recall del modelo de sesión (V18 Fase D).

    full (Opus/Fable 1M) → sin cambios · compact (Sonnet) → top-k reducido + trunc de texto
    · guard_only (Haiku) → solo guards. Fail-open: cualquier error → devuelve res sin tocar.

    Args:
        res: Dict de session_manager.search (semantic/decisions/guards).

    Returns:
        Dict recortado (copia superficial) o el original si el tier es 'full' o falla.
    """
    try:
        if str(ARIS4U_ROOT) not in sys.path:
            sys.path.insert(0, str(ARIS4U_ROOT))
        from tools.model_router import recall_tier, tier_caps

        tier = recall_tier()
        if tier == "full":
            return res
        caps = tier_caps(tier)

        def _trim(items: object, n: int) -> list:
            lst = list(items) if isinstance(items, list) else []
            return lst[:n] if n < 999 else lst

        out = dict(res)
        out["semantic"] = _trim(res.get("semantic"), caps["semantic"])
        out["decisions"] = _trim(res.get("decisions"), caps["decisions"])
        out["guards"] = _trim(res.get("guards"), caps["guards"])
        return out
    except Exception:
        return res


def _append_auto_recall(parts: list[str], query: str, query_type: str = "", cwd: str = "") -> None:
    """AUTO-RECALL (Track A): memoria híbrida (FTS5 + semántico) con cap duro SIGALRM 2s.

    Mismo motor que aris_search. El cap SIGALRM + fail-open garantiza que un Ollama
    lento/caído NUNCA atasque el prompt. Emite telemetría auto_recall (con recall_id para
    casar el marcado de utilidad del freeze) tras correr, pase lo que pase. ``query_type``
    gobierna la inyección de skeleton al build flow (solo en intención de construir/arreglar).

    Args:
        parts: Acumulador de líneas (mutado in-place).
        query: Prompt recortado.
        query_type: Intención clasificada (para la inyección de plantilla).
        cwd: Directorio de trabajo del evento. Habilita el fallback de detección de
            client for projects OUTSIDE the clients dir (aris4u, lab-project-1…), where
            ARIS4U_CLIENT queda vacío pero resolve_client_from_path SÍ resuelve vía
            marcador .aris-client / proyecto conocido. Cierra el 93.4% de recall_events
            sin client_id (Move #1).
    """
    _recall_t0 = time.perf_counter()
    _cli_for_log = ""  # cliente resuelto (env o fallback), reusado en la telemetría
    _recalled = 0
    _n_semantic = 0
    _n_atoms = 0  # átomos levantados por el canal dedicado (boost #4) — telemetría del lazo
    _n_skel = 0  # plantillas (skeleton) inyectadas al build flow
    _injected: list[str] = []  # líneas inyectadas, para el calificador de utilidad
    # ID único del recall: clave primaria para casar el marcado de utilidad
    # (recall_feedback). El calificador automático (tools/recall_usefulness.py) cruza
    # injected + session_id contra la respuesta de Claude para medir utilidad implícita.
    _recall_id = uuid.uuid4().hex[:12]

    def _recall_to(*_a: object) -> None:
        raise TimeoutError()

    try:
        _old_h = signal.signal(signal.SIGALRM, _recall_to)
        signal.alarm(1)  # A0.4: 2s→1s (cap de recall; Ollama lento no atasca el prompt)
        try:
            from engine.v16 import session_manager as _sm
            from engine.v16.config import RECALL_POOL_LIMIT

            _cli = os.environ.get("ARIS4U_CLIENT", "") or None
            # Fallback fail-open (Move #1): ARIS4U_CLIENT solo se setea para 03-clients/;
            # for top-level projects (aris4u, lab-project-1, lab-project-3) is empty and
            # el recall_event se guardaba sin cliente (93.4% NULL). resolve_client_from_path
            # cubre el marcador .aris-client / proyecto conocido. Cualquier error → sin cliente
            # (comportamiento previo), NUNCA bloquea el prompt.
            if not _cli and cwd:
                try:
                    from engine.v16.session_manager import resolve_client_from_path as _rfp

                    _cli = _rfp(cwd)
                except Exception:
                    _cli = None
            _cli_for_log = _cli or ""
            # A0.2 fix: sin cliente conocido → sentinel "" (unscoped-only) en vez de None
            # (que en search() devuelve TODO → fuga cross-client). None se reserva para
            # búsqueda global explícita (aris_search sin scope).
            _search_cid: str | None = _cli if _cli else ""
            # Pool más ancho (boost #4) para que los átomos que pierden el top-3 por
            # similitud cruda igual entren al candidato del split en _build_recall_lines.
            _res = _sm.search(query, limit=RECALL_POOL_LIMIT, client_id=_search_cid)
            # V18 Fase D: recorta el recall al tier del modelo de sesión — no mandar recall
            # pesado a un modelo pequeño (Sonnet=compact, Haiku=guard_only; Opus/Fable=full).
            _res = _tier_recall_result(_res)
            _rl, _n_semantic, _n_atoms, _n_skel = _build_recall_lines(_res, query_type)
            if _rl:
                parts.append("")
                parts.append("🧠 RECALL (memoria ARIS4U relevante):")
                parts.extend(_rl)
                _recalled = len(_rl)
                _injected = list(_rl)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, _old_h)
    except Exception as e:
        # fail-open: nunca bloquear el prompt, pero deja traza si el RECALL falla (no Ollama
        # lento → eso es TimeoutError esperado, sino error real de DB/búsqueda).
        if not isinstance(e, TimeoutError):
            _log_event(
                {
                    "event": "mem_inject_failed",
                    "component": "recall",
                    "error": f"{type(e).__name__}: {str(e)[:160]}",
                }
            )

    # Telemetría auto_recall (observabilidad Track A).
    try:
        _rlat = int((time.perf_counter() - _recall_t0) * 1000)
        _n_chars = sum(len(s) for s in _injected)  # A0.9: observabilidad del volumen inyectado
        _log_event(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "auto_recall",
                "recall_id": _recall_id,
                "results": _recalled,
                "n_semantic": _n_semantic,
                "n_atoms": _n_atoms,
                "n_skeletons": _n_skel,
                "n_chars": _n_chars,
                "tokens_injected": _n_chars // 4,
                "format": "hybrid",
                "query": query[:100],
                "latency_ms": _rlat,
                "client": _cli_for_log or os.environ.get("ARIS4U_CLIENT", ""),
                "session_id": os.environ.get("ARIS4U_SESSION_ID", ""),
                "injected": _injected[:6],
            }
        )
    except Exception:
        pass


def _update_state(parts: list[str], state: dict, query: str, query_type: str) -> None:
    """Persiste el estado de sesión: query_count, GOAL tracking, reset de research.

    Args:
        parts: Acumulador de líneas (mutado: posible GOAL CHECK).
        state: Estado de sesión (mutado in-place y escrito a STATE_FILE).
        query: Prompt recortado.
        query_type: Intención clasificada.
    """
    state.setdefault("query_count", 0)
    state["query_count"] += 1
    state["last_query_type"] = query_type
    state["last_query"] = query[:200]

    # GOAL TRACKING.
    if "session_goal" not in state and query_type != "simple":
        state["session_goal"] = query[:200]
        state["goal_set_at_query"] = state["query_count"]

    if state.get("session_goal") and state["query_count"] >= 20 and state["query_count"] % 20 == 0:
        parts.append("")
        parts.append(f'GOAL CHECK: Still on track with "{state["session_goal"][:50]}..."?')

    # Reset del flag de research para queries de implementación.
    if query_type == "implementation":
        state["research_done_for_current"] = False

    try:
        STATE_FILE.write_text(json.dumps(state))
    except Exception:
        pass


def _log_depth_inject(hook_start: float, query_type: str) -> None:
    """Telemetría depth_inject (latencia del hook) sólo si validation logging activo.

    Args:
        hook_start: Marca perf_counter del inicio del cómputo del hook.
        query_type: Intención clasificada.
    """
    if not (os.environ.get("ARIS4U_VALIDATION_LOG") and os.environ.get("ARIS4U_LOG_FILE")):
        return
    try:
        _hook_latency_ms = int((time.perf_counter() - hook_start) * 1000)
        with open(os.environ["ARIS4U_LOG_FILE"], "a") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "event": "depth_inject",
                        "intent": query_type,
                        "latency_ms": _hook_latency_ms,
                        "session_id": os.environ.get("ARIS4U_SESSION_ID", ""),
                    },
                    default=str,
                )
                + "\n"
            )
    except Exception:
        pass


def _build_context(
    query: str, v16_result: V16QueryResult, depth_on: bool, cwd: str = ""
) -> tuple[list[str], str]:
    """Arma las líneas de additionalContext y persiste el estado de sesión.

    Orquesta, en orden EXACTO al .sh: clasificación → MODEL_HINT → WAVE → TOKEN/EFFORT
    → DEPTH (bajo depth_on) → LOCKED+RECALL (no-simple o sombra) → persistencia de estado.

    Args:
        query: Prompt recortado a 500 chars.
        v16_result: Resultado del orquestador V16.
        depth_on: Si el Depth Protocol está activo (flag ARIS4U_DEPTH_PROTOCOL).
        cwd: Directorio de trabajo del evento (para el fallback de detección de cliente
            del auto-recall en proyectos fuera de 03-clients/).

    Returns:
        Tupla ``(parts, query_type)``: las líneas a emitir y la intención clasificada.
    """
    # Clasificación: query_type + novelty override + rationale + niveles.
    query_type, novelty_override, adaptive_rationale, level_names = _classify_levels(
        query, v16_result
    )

    parts: list[str] = []

    # MODEL ROUTING (advisory) + telemetría.
    _append_model_hint(parts, query_type, novelty_override, v16_result, depth_on)

    # Estado de sesión + WAVE timer + TOKEN/EFFORT.
    state = _load_state()
    _append_wave_timer(parts, state)
    state = _append_token_and_effort(parts, state, query, query_type, depth_on)

    # CORE DEPTH DIRECTIVE (solo si el protocolo está activo; en sombra Claude usa su
    # Adaptive Thinking nativo y ARIS4U no inyecta profundidad).
    if depth_on:
        _append_depth_directive(parts, query_type, level_names, adaptive_rationale)

    # LOCKED DECISIONS + RECALL: si no-simple (protocolo on) o SIEMPRE (modo sombra).
    if (not depth_on) or query_type != "simple":
        _append_client_decisions(parts)
        _append_global_locked(parts, v16_result)
        _append_auto_recall(parts, query, query_type, cwd)

    # Actualizar + persistir estado de sesión (query_count, GOAL, research reset).
    _update_state(parts, state, query, query_type)
    return parts, query_type


def _append_capability_hints(parts: list[str], query: str, query_type: str, cwd: str) -> None:
    """ROUTER (paso 4): sugiere 1-2 capacidades opt-in relevantes + telemetría.

    Cierra la asimetría "lo opt-in se olvida": tras clasificar la intención, matchea el
    prompt contra el catálogo de triggers y, SI hay match de alta confianza, inyecta el
    hint como additionalContext (advisory — Claude decide). Off con ARIS4U_ROUTER=0.
    Fail-open total: el router nunca rompe ni bloquea el prompt.

    Args:
        parts: Acumulador de líneas de additionalContext (mutado in-place).
        query: Prompt recortado.
        query_type: Intención clasificada.
        cwd: Directorio de trabajo (para triggers con contexto de proyecto/cliente).
    """
    if os.environ.get("ARIS4U_ROUTER", "1").strip() == "0":
        return
    try:
        # dispatch.py solo pone hooks/ en sys.path; tools/ vive en el root de aris4u.
        # Sin esto el import fallaba y el except lo tragaba en silencio → el router NUNCA
        # inyectaba en producción (bug latente oculto por el fail-open).
        if str(ARIS4U_ROOT) not in sys.path:
            sys.path.insert(0, str(ARIS4U_ROOT))
        from tools.capability_router import format_hints, route

        hints = route(query, intent=query_type, cwd=cwd, limit=2)
        if not hints:
            return
        block = format_hints(hints)
        if block:
            parts.append("")
            parts.append(block)
        hinted_names = [h["name"] for h in hints]
        _log_event(
            {
                "event": "capability_hint",
                "ts": datetime.now(UTC).isoformat(),
                "intent": query_type,
                "hinted": hinted_names,
                "query": query[:120],
                "session_id": os.environ.get("ARIS4U_SESSION_ID", ""),
            }
        )
        # Fase 4: registra los hints como PENDIENTES de esta sesión para medir adopción
        # (PostToolUse los marca adopted; Stop cierra los ignored). Try/except propio:
        # la telemetría de adopción jamás debe afectar la inyección del hint (fail-open).
        try:
            from tools.capability_adoption import register_hints

            register_hints(os.environ.get("ARIS4U_SESSION_ID", ""), hinted_names, intent=query_type)
        except Exception:
            pass
    except Exception as e:
        # Fail-open (el router nunca debe romper el prompt) PERO no silencioso: un import o
        # router roto debe quedar registrado para no repetir el bug latente de arriba.
        try:
            _log_event(
                {
                    "event": "capability_hint_error",
                    "ts": datetime.now(UTC).isoformat(),
                    "error": f"{type(e).__name__}: {str(e)[:160]}",
                }
            )
        except Exception:
            pass


def _append_orchestration_protocol(parts: list[str], query_type: str) -> None:
    """PROTOCOLO (Fase 3): reencuadra a Claude como orquestador de su toolkit vivo.

    Inyecta, graduado por intención, el ciclo ENTENDER → DISEÑAR → CONSTRUIR →
    VERIFICAR mapeado a las capacidades del inventario VIVO (no una lista hardcoded):
    ``simple``/desconocida → nada; ``fix`` → versión ligera; decision/research/
    implementation → ciclo completo. El techo de Claude Code es que un hook solo puede
    INYECTAR texto que el modelo lee; este es ese texto imperativo, fail-open.

    Off con ARIS4U_ORCH_PROTOCOL=0. Fail-open total: si el inventario/módulo no está,
    no inyecta nada (mejor nada que romper el prompt o nombrar capacidades ausentes).

    Args:
        parts: Acumulador de líneas de additionalContext (mutado in-place).
        query_type: Intención clasificada por F1 (gobierna la graduación).
    """
    if os.environ.get("ARIS4U_ORCH_PROTOCOL", "1").strip() == "0":
        return
    try:
        if str(ARIS4U_ROOT) not in sys.path:
            sys.path.insert(0, str(ARIS4U_ROOT))
        from tools.orchestration_protocol import build_protocol

        block = build_protocol(query_type)
        if not block:
            return
        parts.append("")
        parts.append(block)
        _log_event(
            {
                "event": "capability_protocol",
                "ts": datetime.now(UTC).isoformat(),
                "intent": query_type,
                "chars": len(block),
                "session_id": os.environ.get("ARIS4U_SESSION_ID", ""),
            }
        )
    except Exception as e:
        # Fail-open (el protocolo nunca rompe el prompt) pero no silencioso: un import o
        # builder roto debe quedar registrado para detectarlo (audit fail-open 2026-06-24).
        try:
            _log_event(
                {
                    "event": "capability_protocol_error",
                    "ts": datetime.now(UTC).isoformat(),
                    "error": f"{type(e).__name__}: {str(e)[:160]}",
                }
            )
        except Exception:
            pass


def handle(event_name: str, inp: dict) -> None:
    """Orquestador del hook UserPromptSubmit. Delega cada paso a un helper privado.

    Preserva EXACTO el contrato del .sh: detección/puente de cliente, clasificación
    F1/novelty, MODEL_HINT/DEPTH/EFFORT bajo el flag ARIS4U_DEPTH_PROTOCOL (sombra apaga
    sólo la cognición, el foso RECALL+decisiones sigue vivo), cap SIGALRM 2s del recall,
    side-effects en STATE_FILE, fail-open en cada paso. Salida: additionalContext.
    """
    # Prompt: del payload JSON del evento; fallback a campos alternos por robustez.
    prompt = inp.get("prompt") or inp.get("user_prompt") or inp.get("text") or ""
    if not isinstance(prompt, str):
        prompt = str(prompt)

    # Early-exit idéntico al .sh: prompts triviales (<5 chars) → no-op.
    if len(prompt) < 5:
        passthrough()

    # cwd del evento (espeja session_start); fallback a getcwd.
    cwd = inp.get("cwd") or inp.get("working_directory") or os.getcwd()

    # WS4: detección de cliente + puente MCP + export ARIS4U_CLIENT.
    _setup_client(cwd)

    hook_start = time.perf_counter()

    try:
        from engine.v16.v16_orchestrator import get_orchestrator
    except Exception:
        # Sin engine no hay nada que inyectar — fail-open silencioso.
        passthrough()

    query = prompt[:500]

    # WS-A (modo sombra): ARIS4U_DEPTH_PROTOCOL=0 desactiva la INYECCIÓN de la cognición
    # que Claude hace mejor nativo (DEPTH/EFFORT/MODEL_HINT) y hace que el RECALL corra
    # SIEMPRE. El foso (recall + decisiones per-cliente + guards) sigue vivo. Default '1'
    # = comportamiento actual (freeze-safe). El borrado del cómputo F1/novelty (latencia)
    # llega tras medir el recall en sombra (paso final de WS-A).
    DEPTH_ON = os.environ.get("ARIS4U_DEPTH_PROTOCOL", "1").strip() != "0"

    # V16: pipeline F1→F6 vía orquestador.
    orch = get_orchestrator()
    v16_result = orch.process_query(query)

    # Construcción del contexto + persistencia de estado.
    parts, query_type = _build_context(query, v16_result, DEPTH_ON, cwd)

    # PROTOCOLO (Fase 3): postura de orquestación graduada por intención (el ciclo
    # mapeado al inventario vivo). Va ANTES del router para que el QUÉ/ORDEN se lea
    # primero y el flujo/capacidad específica (más abajo) lo concrete.
    _append_orchestration_protocol(parts, query_type)

    # ROUTER (paso 4): sugiere 1-2 capacidades opt-in relevantes a la tarea.
    _append_capability_hints(parts, query, query_type, cwd)

    # Telemetría depth_inject solo si validation logging activo.
    _log_depth_inject(hook_start, query_type)

    emit_additional_context("\n".join(parts))
