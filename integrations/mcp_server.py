#!/usr/bin/env python3
"""ARIS4U V2.0 MCP Server — 7 tools.

- aris_search:        FTS5 + semantic search across digests/decisions/guards/observations
- aris_ingest:        Decision/guard ingest into sessions.db (locked=True by default so
                      aris_recall_client can retrieve them — V2.0 fix del roundtrip roto)
- aris_recall_client: Per-client recall (decisions, guards, semantic)
- aris_dialectic:     Multi-role Builder/Reviewer/Security review (local Ollama)
- aris_structure:     F1 PRE-amplificación — estructura idea cruda → spec (MoE MLX, opt-in)
- aris_critique:      F1 POST-amplificación — crítica multi-ángulo → FLAGS (MoE MLX, opt-in)
- aris_health:        Cluster health (Mac + W2 Ollama, sessions.db stats)

V2.0 2026-06-11: telemetría JSONL en cada tool (antes: 0 instrumentación — el uso
era invisible), dialectic config-aware (OLLAMA_MAC_URL), casing canónico de cliente.
"""

import hashlib
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import (
    ThreadPoolExecutor,
)
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures import (
    as_completed,
)
from datetime import datetime, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.server.fastmcp import FastMCP

from engine.v16 import method_atom_vocab, model_dispatcher, model_router, session_manager
from engine.v16.config import ARIS4U_ROOT, OLLAMA_MAC_URL, SESSIONS_DB
from tools.project_timeline import ensure_comments_table

mcp = FastMCP("aris4u-v16")

session_manager.init_db()

_EVENTS_LOG = ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"

# Sentinel returned by _call_ollama_role when the local Ollama daemon is
# unreachable (timeout / connection refused), so aris_dialectic can degrade
# gracefully instead of surfacing a raw error as if it were a review.
_OLLAMA_UNAVAILABLE = "__OLLAMA_UNAVAILABLE__"


def _telemetry(tool: str, started: float, **fields) -> None:
    """Emit one JSONL telemetry event per MCP tool call. Fail-silent: telemetry
    must never break a tool response."""
    try:
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "hook": "mcp_server",
            "event": "mcp_tool",
            "tool": tool,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            **fields,
        }
        with _EVENTS_LOG.open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _n(s: str | None) -> str | None:
    """Normaliza un string vacío, de solo espacios o ``None`` a ``None``."""
    return (s or "").strip() or None


def _dup_notice(dups: list) -> str:
    """Aviso de posibles duplicados por firma estructural (el repo PROPONE, Claude DISPONE)."""
    if not dups:
        return ""
    ids = ", ".join(f"#{d['id']}" for d in dups[:5])
    return (
        f"\n⚠️ DEDUP: {len(dups)} átomo(s) con la misma firma estructural ({ids}). "
        f"Si es instancia del mismo método re-ingesta con canonical_id=<id>; "
        f"si es estructuralmente distinto, ignora."
    )


def _ingest_decision(
    content: str,
    domain: str,
    rationale: str,
    locked: bool,
    client_id: str | None,
    problem_class: str,
    artifact_type: str,
    regime: str,
    skeleton: str,
    variable_verdicts: str,
    validity_domain: str,
    transfers_to: str,
    canonical_id: int | None,
    adoption: str,
    evidence_kind: str,
    started: float,
    source_project: str | None = None,
    trust_source: str = "user",
) -> str:
    """Valida los 3 ejes + reglas de ingesta y persiste una decisión / átomo de método.

    Args:
        content: Texto de la decisión (en un átomo de método: el esqueleto estructurado).
        domain: Área de dominio.
        rationale: Por qué / contexto narrativo.
        locked: Si la decisión queda lockeada.
        client_id: Scope de cliente ya normalizado (o None = auto-detect).
        problem_class: Eje 1; si no vacío, marca el item como átomo de método (mem_type='fact').
        artifact_type: Eje 2 (patrón de software).
        regime: Eje 3 (predictibilidad).
        skeleton: JSON con las leyes/métodos.
        variable_verdicts: JSON [{var, verdict, reason}].
        validity_domain: Dónde aplica y dónde rompe.
        transfers_to: JSON [{target_class, rel, condicion}].
        started: perf_counter del inicio del tool (para telemetría).
        trust_source: 'user' (default) or 'audit' — marks audit-generated findings
            so recall can prefix them with '[audit]' and session_end can exclude them
            from the narrative observation (anti-poisoning).

    Returns:
        Mensaje de resultado (guardado o rechazo con motivo).
    """
    pc, at, rg, vd = _n(problem_class), _n(artifact_type), _n(regime), _n(validity_domain)
    ad, ev = _n(adoption), _n(evidence_kind)
    ok, err = method_atom_vocab.validate_method_atom(pc, at, rg, vd, ad, ev)
    cid = client_id or ""
    if not ok:
        _telemetry("aris_ingest", started, content_type="decision", client=cid, rejected=True)
        return f"Rechazado (átomo de método inválido): {err}"

    # Átomo = trae problem_class O artifact_type → mem_type='fact' + firma para dedup.
    is_atom = pc is not None or at is not None
    sig = method_atom_vocab.structural_signature(pc, at, rg) if is_atom else None
    dups = (
        session_manager.find_duplicate_atoms(sig, client_id)
        if (sig and canonical_id is None)
        else []
    )

    session_manager.save_decision(
        decision=content,
        rationale=rationale,
        domain=domain,
        locked=locked,
        client_id=client_id,
        mem_type=("fact" if is_atom else None),
        problem_class=pc,
        artifact_type=at,
        regime=rg,
        skeleton=_n(skeleton),
        variable_verdicts=_n(variable_verdicts),
        validity_domain=vd,
        transfers_to=_n(transfers_to),
        structural_signature=sig,
        canonical_id=canonical_id,
        adoption=ad,
        evidence_kind=ev,
        source_project=_n(source_project),
        trust_source=trust_source or "user",
    )
    _telemetry(
        "aris_ingest",
        started,
        content_type="decision",
        client=cid,
        locked=locked,
        problem_class=pc or "",
    )
    return (
        f"Decision saved{' (locked)' if locked else ''}: {content[:100]} (domain: {domain})"
        + _dup_notice(dups)
    )


@mcp.tool()
def aris_ingest(
    content: str,
    content_type: str = "decision",
    domain: str = "",
    rationale: str = "",
    client: str = "",
    locked: bool = True,
    problem_class: str = "",
    artifact_type: str = "",
    regime: str = "",
    skeleton: str = "",
    variable_verdicts: str = "",
    validity_domain: str = "",
    transfers_to: str = "",
    canonical_id: int = 0,
    adoption: str = "",
    evidence_kind: str = "",
    source_project: str = "",
    trust_source: str = "",
) -> str:
    """Store a decision or guard for future sessions.

    Args:
        content: The decision text or guard pattern
        content_type: 'decision' or 'guard'
        domain: Domain area (auth, database, security, etc.)
        rationale: Why this decision was made
        client: Scope this memory to a client (e.g. acme-corp). Empty = auto-detect from cwd/ARIS4U_CLIENT.
        locked: Lock the decision (default True — locked decisions propagate to subagents and client recall)
        problem_class: Átomo de método eje 1 — estructura del problema (ver method_atom_vocab).
            Si se da, el item se marca mem_type='fact' y exige regime + validity_domain.
        artifact_type: Átomo de método eje 2 — patrón de software (opcional).
        regime: Átomo de método eje 3 — predictibilidad. 'pure-random' se rechaza (sin método acumulable).
        skeleton: JSON con las leyes/métodos del modelo.
        variable_verdicts: JSON [{var, verdict KEEP/DEPENDS/DISCARD, reason}].
        validity_domain: Dónde aplica y dónde rompe el esqueleto (obligatorio si problem_class).
        transfers_to: JSON [{target_class, rel, condicion}] — transfiere el esqueleto, no la calibración.
        canonical_id: Si este átomo es instancia de uno canónico ya guardado, su id (0 = ninguno).
        adoption: Estado de adopción: used | used-naive | unused | gap-no-method-exists.
        evidence_kind: Origen: calibrated (medido en runtime) | catalog (conocimiento puro del estado del arte).
        source_project: Proyecto de ORIGEN del átomo (mi-proyecto, aris4u…). Vacío =
            auto-detect del cwd/puente de sesión. Alimenta el grafo de transferencia entre proyectos.
        trust_source: Procedencia. '' or 'user' = decisión del usuario (default).
            'audit' = hallazgo de aris-client-audit. El recall prefija con '[audit]';
            session_end lo excluye de la narrativa (anti-poisoning).
    """
    started = time.perf_counter()
    # G6 fix: '' y '   ' → None (auto-detect); ya no se condiciona con `if client`.
    client_id = client.strip().lower() or None
    if content_type == "guard":
        session_manager.save_guard(
            pattern=content,
            prevention=rationale or "See original session for details",
            source_session="current",
            severity="high",
            client_id=client_id,
        )
        _telemetry("aris_ingest", started, content_type="guard", client=client_id or "")
        return f"Guard saved: {content[:100]}"

    return _ingest_decision(
        content,
        domain,
        rationale,
        locked,
        client_id,
        problem_class,
        artifact_type,
        regime,
        skeleton,
        variable_verdicts,
        validity_domain,
        transfers_to,
        (canonical_id or None),
        adoption,
        evidence_kind,
        started,
        source_project=source_project,
        trust_source=trust_source or "user",
    )


@mcp.tool()
def aris_search(query: str, client: str = "") -> str:
    """Full-text + semantic search across digests, decisions, guards, and observations.

    Pass client to scope semantic recall to one client (e.g. acme-corp); empty = all.
    """
    started = time.perf_counter()
    client_id = (client.strip().lower() or None) if client else None
    results = session_manager.search(query, limit=10, client_id=client_id)
    parts = []

    for d in results.get("digests", []):
        parts.append(f"[{d['id']}] {d.get('summary', '')[:200]}")
        if d.get("decisions"):
            parts.append(f"  Decisions: {d['decisions'][:200]}")

    for d in results.get("decisions", []):
        prefix = "[audit] " if d.get("trust_source") == "audit" else ""
        parts.append(f"Decision ({d.get('domain', '-')}): {prefix}{d['decision'][:200]}")

    for g in results.get("guards", []):
        parts.append(f"Guard: {g['pattern'][:100]} → {g['prevention'][:100]}")

    for s in results.get("semantic", []):
        parts.append(f"~{s['similarity']:.2f} [{s['source']}#{s['source_id']}] {s['text'][:200]}")

    n_results = len(parts)
    _telemetry("aris_search", started, client=client_id or "", results=n_results)
    if not parts:
        return f"No results for '{query}'."

    return "\n".join(parts)


def _call_ollama_role(role: str, prompt: str, timeout: int = 45) -> tuple[str, str]:
    """Call a local model for a given role prompt, via the multi-model router.

    Delega en `model_router.route_local("dialectic", …)`, que es health-aware
    (no intenta modelos no instalados — antes se hardcodeaban dos), añade W2 como
    tercer fallback si el Mac está caído, y registra telemetría `model_route`.
    El review dialéctico SIEMPRE es local (PHI-safe por construcción).

    Args:
        role: Role name (builder, reviewer, security)
        prompt: The prompt to send
        timeout: Inference timeout in seconds (per attempt)

    Returns:
        Tuple of (role, response_text[:1000]) o (role, _OLLAMA_UNAVAILABLE) si
        ningún modelo local respondió — el caller degrada con gracia.
    """
    res = model_router.route_local("dialectic", prompt, timeout=timeout)
    if res.ok and res.text:
        return (role, res.text.strip()[:1000])
    # Nada local respondió (daemon caído o sin modelo vivo): señal de
    # indisponibilidad para que aris_dialectic degrade limpio, no como hallazgo.
    return (role, _OLLAMA_UNAVAILABLE)


def _dialectic_build_prompts(task: str, file_path: str) -> dict[str, str]:
    """Return per-role prompts for aris_dialectic. file_path is appended when present."""
    suffix = f"\nFile: {file_path}" if file_path else ""
    _ = f"Review this task: {task}{suffix}"  # context (kept for future use)
    return {
        "builder": f"As a Builder, implement or verify: {task}. Focus on correctness and completeness.",
        "reviewer": f"As a Code Reviewer, find bugs, edge cases, and improvements in: {task}. Be thorough.",
        "security": f"As a Security Auditor, find vulnerabilities in: {task}. Check OWASP top 10, injection, auth issues.",
    }


def _dialectic_run_roles(prompts: dict[str, str]) -> dict[str, str]:
    """Dispatch all roles to local Ollama in parallel.

    Returns an empty dict if the overall timeout fires (daemon unreachable).
    """
    results: dict[str, str] = {}
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_call_ollama_role, role, prompt, 90): role
                for role, prompt in prompts.items()
            }
            for future in as_completed(futures, timeout=100):
                role_name, result = future.result()
                results[role_name] = result
    except FuturesTimeoutError:
        # No role returned in time — treat as the daemon being unavailable.
        results = {}
    return results


def _dialectic_format_output(results: dict[str, str]) -> tuple[str, int]:
    """Render per-role results and count finding-bearing roles.

    Returns (formatted_output, findings_count).
    """
    _finding_keywords = ["bug", "vulnerability", "issue", "error", "fix"]
    output = []
    for role, result in results.items():
        if result == _OLLAMA_UNAVAILABLE:
            result = f"{role}: no disponible (Ollama local no respondió)"
        output.append(f"=== {role.upper()} ===\n{result}\n")

    findings_count = sum(
        1
        for r in results.values()
        if r != _OLLAMA_UNAVAILABLE and any(w in r.lower() for w in _finding_keywords)
    )
    output.append(f"\n--- {findings_count} roles found issues ---")
    return "\n".join(output), findings_count


# ── V18 Fase B: reconversión del perímetro a Claude (harness-delegado) ────────────
# Sin ANTHROPIC_API_KEY (Max = OAuth), el engine no llama a la API de Claude. La forma
# fiel a la decisión "reconvertir a Claude, local solo PHI" es DELEGAR AL HARNESS: el tool
# devuelve prompts de rol expertos para que Claude los ejecute como subagentes Sonnet en
# paralelo (99% disponibilidad, calidad muy superior al 8B local). El review local Ollama
# se RESERVA para contexto healthcare/PHI (extra-cautela). Mata el 85% de fallos de dialectic.
def _is_healthcare() -> bool:
    """True si la sesión está en contexto healthcare (switch maestro ARIS4U_HEALTHCARE=1)."""
    return os.environ.get("ARIS4U_HEALTHCARE", "0").strip() == "1"


def _delegated_dialectic(task: str, file_path: str, prompts: dict[str, str]) -> str:
    """Directiva harness-delegada: Claude ejecuta el review como 3 subagentes Sonnet."""
    tgt = f"\n  Archivo: {file_path}" if file_path else ""
    return (
        "🔀 ARIS4U DIALECTIC → ejecútalo como review multi-rol con subagentes **Sonnet** (V18).\n"
        'Lanza 3 `Agent(model="sonnet")` EN PARALELO (un solo mensaje), uno por rol, sobre:\n'
        f"  Tarea: {task}{tgt}\n"
        f"  1. BUILDER  — {prompts['builder']}\n"
        f"  2. REVIEWER — {prompts['reviewer']}\n"
        f"  3. SECURITY — {prompts['security']}\n"
        "Cada subagente devuelve hallazgos concretos (archivo:línea + severidad). Luego TÚ "
        "sintetizas los 3 en un veredicto. (El review local Ollama se reserva para healthcare/PHI.)"
    )


@mcp.tool()
def aris_dialectic(task: str, file_path: str = "") -> str:
    """Multi-role review: Builder + Reviewer + Security analyze in parallel.

    Use for critical code that needs thorough review. Fuera de healthcare, delega el review
    a subagentes Sonnet del harness (V18); en healthcare corre local (Ollama, PHI-safe).
    """
    started = time.perf_counter()
    prompts = _dialectic_build_prompts(task, file_path)

    # V18: no-healthcare → harness-delegado a Sonnet (disponibilidad ~99%).
    if not _is_healthcare():
        _telemetry("aris_dialectic", started, roles=3, findings=0, backend="harness-sonnet",
                   mode="harness-delegated", n_subagents_est=3)
        return _delegated_dialectic(task, file_path, prompts)

    # Healthcare → review local Ollama (PHI-safe), con degradación elegante.
    results = _dialectic_run_roles(prompts)

    # Graceful degradation: if every role failed because the local Ollama
    # daemon is unreachable (or none responded in time), return a clear,
    # actionable message instead of three raw error strings dressed up as a
    # review with a misleading "0 roles found issues" footer.
    unavailable_roles = [r for r in results.values() if r == _OLLAMA_UNAVAILABLE]
    if not results or len(unavailable_roles) == len(results):
        _telemetry("aris_dialectic", started, roles=0, findings=0, ollama="unavailable",
                   mode="local-ollama", n_subagents_est=0)
        return (
            "⚠️ Dialéctica no disponible: Ollama local no responde "
            f"(timeout/conexión rechazada en {OLLAMA_MAC_URL}).\n"
            "El review multi-rol (Builder/Reviewer/Security) corre sobre modelos "
            "locales de Ollama. Arranca el daemon (`ollama serve`) y reintenta, "
            "o revisa el código manualmente / con un subagente.\n"
            "Esto NO es un hallazgo de seguridad — es indisponibilidad del motor local."
        )

    output, findings_count = _dialectic_format_output(results)
    _telemetry("aris_dialectic", started, roles=len(results), findings=findings_count,
               mode="local-ollama", n_subagents_est=0)
    return output


# ── F1: amplificador local de I/O (LOCAL_AMPLIFIER_BLUEPRINT §4-5) ────────────────
# OPT-IN. El cuerpo local (Qwen3.6-35B-A3B vía MLX) PROPONE; Claude/el usuario DISPONEN. Si el
# server MLX está frío, route_local devuelve ok=False → se OMITE la amplificación
# (blueprint #5: nunca se degrada al 8B de seguridad ni se bloquea el trabajo).

_AMPLIFY_UNAVAILABLE = (
    "⚠️ Amplificación local no disponible: el cuerpo local (Qwen3.6-35B-A3B vía MLX) no "
    "responde. Arráncalo con `tools/mlx_serve.sh start` o continúa sin amplificar (usa "
    "el texto tal cual). Esto NO es un error de tu trabajo — el motor local opt-in está apagado."
)
_AMPLIFY_NOTE = "— Sugerencia del cuerpo local (a filtrar, NO es veredicto de Claude) —"
_STRUCTURE_SYSTEM = (
    "Eres un asistente que ESTRUCTURA una idea cruda en una especificación accionable. "
    "NO la resuelvas ni juzgues su correctitud — solo organízala. Empieza DIRECTO, SIN "
    "preámbulo ni cierre. Secciones con viñetas BREVES (no párrafos): OBJETIVO, "
    "REQUISITOS, RIESGOS, CRITERIOS DE ACEPTACIÓN, ÁNGULOS A CONSIDERAR. Máximo ~4 "
    "viñetas por sección, las más importantes. Si la idea ya es una spec clara, dilo y "
    "no inventes relleno."
)
_CRITIQUE_SYSTEM = (
    "Eres un crítico que señala BANDERAS (flags) concretas en una respuesta o código, "
    "desde varios ángulos. NO emitas un veredicto final de correctitud — solo lista "
    "problemas POTENCIALES a revisar, agrupados por ángulo. Empieza DIRECTO, SIN "
    "preámbulo ni cierre. Máximo ~3 banderas por ángulo, las más importantes, en viñetas "
    "breves y específicas. Si no ves problemas en un ángulo, dilo en una línea."
)
_DEFAULT_ANGLES = "correctitud,seguridad,lógica,omisiones,mantenibilidad"


def _looks_structured(text: str) -> bool:
    """Heurística anti query-drift: ¿el input ya parece una spec estructurada?

    Re-estructurar algo ya claro arriesga query-drift (23-42%, blueprint §2) → mejor
    pasarlo tal cual.
    """
    lowered = text.lower()
    has_sections = sum(kw in lowered for kw in ("objetivo", "requisito", "criterio", "riesgo")) >= 2
    bullet_lines = sum(
        1 for ln in text.splitlines() if ln.strip()[:2] in ("- ", "* ", "1.", "2.", "# ")
    )
    return has_sections or bullet_lines >= 3


def _amplify_return(tool: str, started: float, res, **extra: object) -> str:
    """Telemetría enriquecida (con call_id) + salida F1 etiquetada.

    El call_id correlaciona el feedback de utilidad (tools/f1_feedback.py) con esta
    llamada concreta → datos para medir el ROI de F1 (blueprint F2 / tools/f1_roi.py).
    """
    text = res.text.strip()
    call_id = hashlib.sha1(f"{tool}|{text}|{started}".encode()).hexdigest()[:8]
    _telemetry(
        tool,
        started,
        available=True,
        chars=len(text),
        backend=res.backend,
        call_id=call_id,
        promise_score=getattr(res, "promise_score", None),
        mode="local-ollama",
        n_subagents_est=0,
        **extra,
    )
    return f"{_AMPLIFY_NOTE}  ·  F1 id:{call_id}\n\n{text}"


@mcp.tool()
def aris_structure(idea: str) -> str:
    """PRE-amplificación (opt-in): estructura una idea cruda en una spec accionable.

    El cuerpo local (Qwen3.6-35B-A3B) expande una idea en objetivo/requisitos/riesgos/
    criterios/ángulos para que Claude trabaje sobre una entrada más rica. NO resuelve ni
    juzga. Si la idea ya está estructurada, la pasa tal cual (anti query-drift). Si el
    server MLX está frío, devuelve un aviso (omite, no bloquea).
    """
    started = time.perf_counter()
    # A0.3: call_id estable al inicio — el fallback (Ollama frío) carecía de él,
    # dejando esas llamadas invisibles a f1_label.py. Se replica el patrón de _amplify_return.
    _call_id = hashlib.sha1(f"aris_structure|{idea[:200]}|{started}".encode()).hexdigest()[:8]
    if _looks_structured(idea):
        _telemetry("aris_structure", started, available=True, skipped="already_structured",
                   mode="local-noop", n_subagents_est=0)
        return f"(La idea ya parece estructurada; sin cambios.)\n\n{idea}"
    # INERT: mlx_lm.server no servido; cae a Sonnet siempre; 0 labels. Fable-Gate 2026-07-05.
    res = model_router.route_local(
        "structure_prompt", idea, system=_STRUCTURE_SYSTEM, timeout=60, want_score=True
    )
    if not (res.ok and res.text):
        # V18: fuera de healthcare, delega a Sonnet en vez del dead-end (el cuerpo local
        # MLX casi nunca está caliente). En healthcare se mantiene local (PHI).
        if not _is_healthcare():
            _telemetry("aris_structure", started, available=True, backend="harness-sonnet", call_id=_call_id,
                       mode="harness-delegated", n_subagents_est=1)
            return (
                "🔀 ARIS4U STRUCTURE → cuerpo local frío; estructura con un subagente Sonnet:\n"
                '`Agent(model="sonnet", prompt="Estructura esta idea cruda en objetivo / '
                "requisitos / riesgos / criterios / ángulos, SIN resolverla ni juzgarla:\\n\\n"
                f'{idea}")`  — o hazlo inline si es breve.\n'
                f"F1 id:{_call_id}"
            )
        _telemetry("aris_structure", started, available=False, call_id=_call_id,
                   mode="local-ollama", n_subagents_est=0)
        return f"{_AMPLIFY_UNAVAILABLE}  ·  F1 id:{_call_id}"
    return _amplify_return("aris_structure", started, res)


@mcp.tool()
def aris_critique(response: str, angles: str = _DEFAULT_ANGLES) -> str:
    """POST-amplificación (opt-in): critica una respuesta/código multi-ángulo → FLAGS.

    El cuerpo local revisa desde los ángulos dados y devuelve BANDERAS a filtrar — NO un
    veredicto de correctitud (eso es de Claude/el usuario, blueprint §2). Una sola pasada
    multi-ángulo (el mlx_lm.server serializa; N llamadas paralelas no acelerarían). Si el
    server MLX está frío, omite (no bloquea).

    Args:
        response: La respuesta o código a criticar.
        angles: Ángulos separados por coma (default: correctitud/seguridad/lógica/omisiones/mantenibilidad).
    """
    started = time.perf_counter()
    # A0.3: call_id estable al inicio — el fallback carecía de él.
    _call_id = hashlib.sha1(f"aris_critique|{response[:200]}|{started}".encode()).hexdigest()[:8]
    angle_list = [a.strip() for a in angles.split(",") if a.strip()]
    prompt = (
        f"Ángulos a revisar: {', '.join(angle_list)}.\n"
        f"Lista banderas concretas por ángulo en lo siguiente:\n\n{response}"
    )
    # INERT: mlx_lm.server no servido; cae a Sonnet siempre; 0 labels. Fable-Gate 2026-07-05.
    res = model_router.route_local(
        "critique", prompt, system=_CRITIQUE_SYSTEM, timeout=90, want_score=True
    )
    if not (res.ok and res.text):
        # V18: fuera de healthcare, delega a un subagente Sonnet independiente (segundo par
        # de ojos real) en vez del dead-end. En healthcare se mantiene local (PHI).
        if not _is_healthcare():
            _telemetry(
                "aris_critique",
                started,
                available=True,
                angles=len(angle_list),
                backend="harness-sonnet",
                call_id=_call_id,
                mode="harness-delegated",
                n_subagents_est=1,
            )
            return (
                "🔀 ARIS4U CRITIQUE → cuerpo local frío; critica con un subagente Sonnet "
                "independiente:\n"
                f'`Agent(model="sonnet", prompt="Lista banderas concretas por ángulo '
                f"({', '.join(angle_list)}) en lo siguiente — NO des veredicto de correctitud, "
                f'solo banderas a filtrar:\\n\\n{response}")`\n'
                f"F1 id:{_call_id}"
            )
        _telemetry("aris_critique", started, available=False, angles=len(angle_list), call_id=_call_id,
                   mode="local-ollama", n_subagents_est=0)
        return f"{_AMPLIFY_UNAVAILABLE}  ·  F1 id:{_call_id}"
    return _amplify_return("aris_critique", started, res, angles=len(angle_list))


@mcp.tool()
def aris_health() -> str:
    """System health check — both machines, all models, sessions.db stats, W2 Docker containers."""
    started = time.perf_counter()
    health = model_dispatcher.health_check()
    stats = session_manager.get_stats()

    lines = ["=== ARIS4U Health ==="]
    lines.append(f"Mac Ollama: {'UP' if health['mac']['ollama'] else 'DOWN'}")
    for m in health["mac"]["models"]:
        lines.append(f"  - {m}")

    lines.append(f"W2 Ollama: {'UP' if health['w2']['ollama'] else 'DOWN'}")
    for m in health["w2"]["models"]:
        lines.append(f"  - {m}")

    # W2 Docker container monitoring
    EXPECTED_W2_SERVICES = [
        "supabase",
        "kong",
        "auth",
        "rest",
        "realtime",
        "storage",
        "imgproxy",
        "inbucket",
        "postgres",
        "meta",
        "umami",
        "n8n",
    ]
    try:
        import subprocess as _sp

        r = _sp.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=6",
                "-o",
                "BatchMode=yes",
                "w2",
                "docker ps --format '{{.Names}}\t{{.Status}}'",
            ],
            capture_output=True,
            text=True,
            timeout=12,
        )
        if r.returncode == 0 and r.stdout.strip():
            running = [line.split("\t")[0] for line in r.stdout.strip().splitlines()]
            lines.append(f"W2 Docker: {len(running)} containers up")
            missing = [s for s in EXPECTED_W2_SERVICES if not any(s in n for n in running)]
            if missing:
                lines.append(f"  ⚠️ EXPECTED MISSING: {', '.join(missing)}")
            for name_status in r.stdout.strip().splitlines()[:15]:
                lines.append(f"  {name_status}")
        else:
            lines.append(f"W2 Docker: ERROR (ssh exit {r.returncode})")
    except Exception as _e:
        lines.append(f"W2 Docker: TIMEOUT/UNREACHABLE ({type(_e).__name__})")

    lines.append(
        f"sessions.db: {stats['digests']} digests, {stats['decisions']} decisions, {stats['guards']} guards"
    )

    _telemetry("aris_health", started, mac_up=health["mac"]["ollama"], w2_up=health["w2"]["ollama"])
    return "\n".join(lines)


# Ensure cowork_comments table is created at most once per process.
# aris_recall_client is invoked on every session-start; paying a CREATE TABLE
# IF NOT EXISTS + commit on every call is unnecessary write traffic on a read tool.
_COWORK_TABLE_READY: bool = False


def _recall_cowork(db: sqlite3.Connection, canonical: str, limit: int) -> list[str]:
    """Return formatted cowork feedback lines for *canonical* client, most-recent first.

    Queries ``cowork_comments`` using the provided open connection.  Returns an
    empty list when there are no comments — callers insert the section header
    only when this is non-empty.  SQL is fully parametrised; isolation by
    ``client_id`` is enforced in the WHERE clause.

    Args:
        db: Open SQLite connection (row_factory = sqlite3.Row).
        canonical: Lower-cased client_id to scope the query.
        limit: Maximum rows to return.

    Returns:
        List of formatted strings, one per comment.  Empty list if none found.
    """
    rows = db.execute(
        "SELECT commit_sha, author, role, body FROM cowork_comments "
        "WHERE client_id = ? "
        "ORDER BY created_at DESC LIMIT ?",
        [canonical, limit],
    ).fetchall()
    out = []
    for row in rows:
        sha_short = (row[0] or "unknown")[:7]
        out.append(f"[{sha_short}] {row[1]} ({row[2]}): {row[3][:200]}")
    return out


@mcp.tool()
def aris_recall_client(client_name: str, query: str = "", limit: int = 5, tier: str = "") -> str:
    """Recall locked decisions and guards scoped by client_id. (WS4 per-client memory).

    Args:
        client_name: Client identifier (e.g. acme-corp)
        query: Optional search query to filter decisions/guards
        limit: Max number of results per category
        tier: V18 recall tier ('full'|'compact'|'guard_only'). '' = usa limit tal cual.
              compact recorta; guard_only devuelve solo guards (para subagentes Haiku/Sonnet).

    Returns:
        Formatted list of client-scoped decisions and guards
    """
    started = time.perf_counter()
    canonical = client_name.strip().lower()
    n_dec = n_guards = n_sem = n_cowork = 0
    # V18 Fase D: si se pide un tier, deriva límites por categoría (no mandar recall pesado
    # a un modelo pequeño). Sin tier → comportamiento clásico (limit para las tres).
    dec_limit = guard_limit = sem_limit = limit
    cowork_limit = limit  # default: same as limit; guard_only tier sets this to 0
    if tier:
        try:
            from tools.model_router import tier_caps

            _c = tier_caps(tier)
            dec_limit = min(limit, _c["decisions"])
            guard_limit = min(limit, _c["guards"])
            sem_limit = min(limit, _c["semantic"])
            cowork_limit = min(limit, _c.get("cowork", limit))
        except Exception:
            pass
    db = session_manager._connect()
    parts = [f"=== {canonical} Decisions ==="]

    try:
        # V2.0 fix P0 roundtrip: NO filtrar locked=1 (aris_ingest histórico guardó con
        # locked=0 y esas decisiones eran irrecuperables). Locked primero, luego recientes.
        # Dedup lógico (2026-06-29): excluir copias no-canónicas de facts para no traer
        # duplicados; el filtro NO toca decisiones normales (mem_type != 'fact').
        sql = (
            "SELECT decision, rationale, domain, session_ref, locked, trust_source FROM decisions "
            "WHERE client_id = ? "
            # FIX #7: canonical_id = '' removido (INTEGER, siempre falso — dead code).
            "AND (mem_type != 'fact' OR canonical_id IS NULL OR canonical_id = id) "
            "ORDER BY locked DESC, created_at DESC LIMIT ?"
        )
        rows = db.execute(sql, [canonical, dec_limit]).fetchall() if dec_limit > 0 else []

        if rows:
            n_dec = len(rows)
            for row in rows:
                ref = row[3] or "unref"
                lock = "🔒 " if row[4] else ""
                audit_prefix = "[audit] " if row[5] == "audit" else ""
                parts.append(f"{lock}[{ref}] ({row[2] or 'general'}): {audit_prefix}{row[0][:150]}")
                if row[1]:
                    parts.append(f"  Rationale: {row[1][:100]}")
        else:
            parts.append(f"(No decisions for {canonical})")
    except Exception as e:
        parts.append(f"Error querying decisions: {str(e)[:100]}")

    parts.append("")
    parts.append(f"=== {canonical} Guards ===")

    try:
        sql = "SELECT pattern, prevention, severity FROM guards WHERE client_id = ? ORDER BY created_at DESC LIMIT ?"
        rows = db.execute(sql, [canonical, guard_limit]).fetchall()

        if rows:
            n_guards = len(rows)
            for row in rows:
                parts.append(f"[{row[2]}] {row[0][:100]} → {row[1][:100]}")
        else:
            parts.append(f"(No guards for {canonical})")
    except Exception as e:
        parts.append(f"Error querying guards: {str(e)[:100]}")
    finally:
        db.close()

    # Increment 4: cowork_comments — commit-anchored feedback scoped by client_id.
    # Semantically distinct from decisions/guards (not a policy, not a rule — it is
    # reviewer feedback tied to a specific commit SHA). Lives in its own section so
    # the dev sees it as actionable context at session start ("reviewer said X on abc123").
    # Uses a separate connection so the guards try/finally above stays untouched.
    if cowork_limit > 0:
        try:
            global _COWORK_TABLE_READY
            if not _COWORK_TABLE_READY:
                ensure_comments_table(SESSIONS_DB)
                _COWORK_TABLE_READY = True
            _cw_db = session_manager._connect()
            try:
                _cw_lines = _recall_cowork(_cw_db, canonical, cowork_limit)
            finally:
                _cw_db.close()
            if _cw_lines:
                n_cowork = len(_cw_lines)
                parts.append("")
                parts.append(f"=== {canonical} Cowork Feedback ===")
                parts.extend(_cw_lines)
        except Exception as _e:
            parts.append(f"(cowork_comments unavailable: {str(_e)[:80]})")

    # V2.0 fix casing: el recall semántico recibía client_name crudo (e.g. "acme-corp")
    # mientras la DB guarda client_id canónico lower-case → 0 resultados siempre.
    sem = (
        session_manager.semantic_recall(query or canonical, client_id=canonical, limit=sem_limit)
        if sem_limit > 0
        else []
    )
    if sem:
        n_sem = len(sem)
        parts.append("")
        parts.append(f"=== {canonical} Semantic Recall ===")
        for s in sem:
            parts.append(
                f"~{s['similarity']:.2f} [{s['source']}#{s['source_id']}] {s['text'][:150]}"
            )

    _telemetry(
        "aris_recall_client",
        started,
        client=canonical,
        decisions=n_dec,
        guards=n_guards,
        semantic=n_sem,
        cowork=n_cowork,
    )
    return "\n".join(parts)


if __name__ == "__main__":
    mcp.run()
