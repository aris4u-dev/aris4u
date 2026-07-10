"""Capa de routing multi-modelo de ARIS4U — Fase A (LOCAL-ONLY).

Principio rector (ver architecture/ARIS4U_MASTER.md + memoria
project_aris4u_multimodel_layer): **se enruta la PERIFERIA, nunca la cognición.**
Claude (el harness de Claude Code) sigue siendo el cerebro para razonamiento,
código complejo y multi-paso agéntico. Esta capa unifica el acceso a los modelos
LOCALES (Ollama Mac / W2) detrás de una sola política declarativa con:

  - **Privacidad por construcción:** `route_local` SOLO toca backends locales
    (Mac/W2 Ollama). Nada de lo que pasa por aquí sale del host — es el building
    block PHI-safe sobre el que Fase C montará routing online con un gate
    fail-closed (`route()`, todavía NO existe; ver al final).
  - **Resiliencia / health-awareness:** antes de invocar un modelo se verifica
    que esté VIVO (Ollama `/api/tags`). Esto absorbe el drift config↔realidad
    (un modelo nombrado en config/política que no esté instalado o se renombre).
    Fallback en
    cascada Mac→W2→fail-open. El backend W2 (ssh, caro) se consulta de forma
    perezosa: si un candidato Mac resuelve, W2 nunca se toca.
  - **Telemetría:** cada ruteo emite un evento `model_route` a
    logs/v16.1-events.jsonl (backend, modelo, ok, fallback, latencia). Es la
    observabilidad anti-colapso/drift del plan (sin router ML).

Fase B añadirá tasks de costo (summarize/extract/draft) y sus callers. Fase C
añadirá backends ONLINE (Gemini/Grok) detrás de un `route()` con privacy
fail-closed. Hoy NO se registra ningún backend externo — cero credenciales
nuevas, cero fuga posible. El diseño es extensible; la superficie de hoy no.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, UTC
from typing import Optional

from .config import (
    ARIS4U_ROOT,
    OLLAMA_MAC_URL,
    OLLAMA_W2_URL,
    W2_ENABLED,
    W2_SSH,
    MLX_MODEL,
    MLX_URL,
)
from .model_dispatcher import dispatch_local, dispatch_w2, dispatch_grok, dispatch_mlx

# Backends que `route_local` tiene PERMITIDO tocar. Invariante de privacidad:
# cualquier candidato cuyo backend no esté aquí se salta. `route_local` lee SOLO
# `_POLICY` (local); los backends online viven en `_ONLINE_POLICY`, físicamente
# separados, así que route_local nunca puede alcanzarlos ni por accidente.
_LOCAL_BACKENDS = ("mac", "w2", "mlx")
_ONLINE_BACKENDS = ("grok",)

_CACHE_TTL = 60.0  # segundos que se cachea una lista NO vacía de modelos vivos
# Cache negativo más corto: un set() suele ser un blip (Ollama recién caído / ssh
# lento), no "0 modelos". Re-consultar pronto evita desviar dialectic a W2 60s
# cuando el Mac ya volvió.
_CACHE_TTL_EMPTY = 10.0


@dataclass
class Candidate:
    """Un (backend, modelo) a intentar, con sus opciones de inferencia Ollama."""

    backend: str
    model: str
    options: dict = field(default_factory=dict)


@dataclass
class RouteResult:
    """Resultado de un ruteo. `ok=False` + `text=None` = fail-open (nada respondió)."""

    text: Optional[str]
    backend: str
    model: str
    ok: bool
    fallback_used: bool
    latency_ms: int
    candidates_tried: int
    error: str = ""
    promise_score: Optional[float] = None  # confianza del cuerpo (solo mlx + want_score)


# Política declarativa (SIN ML). Lista ORDENADA de candidatos por task; el primero
# que esté vivo y responda gana. Los modelos se nombran explícitos (no vía los
# defaults de config) para que la health-awareness tenga un nombre real contra el
# cual validar.
#
# Callers de producción: `dialectic` (aris_dialectic, Fase A) y `digest`
# (session_end narrative, Fase B). `default` = red de seguridad genérica. Cada
# task se añade SOLO cuando tiene caller real — no dejar política muerta. Fase B
# seguirá con summarize/extract si aparecen sus callers.
# 2026-06-18 V2.0 Fase 3a: los 3 generativos Mac del catálogo viejo (qwen35-analyst,
# gemma4-abliterated, qwen2.5:7b-instruct) fueron BORRADOS el 06-16 → el router caía
# callado a W2 (fail-open en Mac-only/W2-down). Reapuntado a lo VIVO: Foundation-Sec-8B
# (Mac, local-first, PHI-safe; modelo de seguridad que encaja con el framing ofensivo)
# + qwen3:8b (W2) de fallback. Fase 3b instalará Qwen3.6-35B-A3B (MLX) como primario Mac y
# lo reemplazará aquí.
_FSEC = "hf.co/roadus/Foundation-Sec-8B-Q4_K_M-GGUF:latest"
# Fase 3b: el cuerpo local Qwen3.6-35B-A3B (backend "mlx") es el PRIMARIO de cada tarea —
# health-aware: si el mlx_lm.server NO corre, _live_models("mlx")=set() → se salta y cae
# a Foundation-Sec Mac y luego W2 (fail-open). Cascada de calidad: MoE → Foundation-Sec → W2.
_POLICY: dict[str, list[Candidate]] = {
    "dialectic": [
        Candidate("mlx", MLX_MODEL, {"temperature": 0.3, "num_predict": 400}),
        Candidate("mac", _FSEC, {"temperature": 0.3, "num_predict": 400}),
        Candidate("w2", "qwen3:8b", {"temperature": 0.3, "num_predict": 400}),
    ],
    # digest: frase corta y factual al cierre de sesión. MoE si está caliente; si no,
    # W2 qwen3:8b (general, ligero) y Foundation-Sec Mac de respaldo (no quedar W2-only).
    "digest": [
        Candidate("mlx", MLX_MODEL, {"temperature": 0.2, "num_predict": 80}),
        Candidate("w2", "qwen3:8b", {"temperature": 0.2, "num_predict": 80}),
        Candidate("mac", _FSEC, {"temperature": 0.2, "num_predict": 80}),
    ],
    "default": [
        Candidate("mlx", MLX_MODEL, {"temperature": 0.3, "num_predict": 400}),
        Candidate("mac", _FSEC, {"temperature": 0.3, "num_predict": 400}),
        Candidate("w2", "qwen3:8b", {"temperature": 0.3, "num_predict": 400}),
    ],
    # F1 amplificador de I/O (LOCAL_AMPLIFIER_BLUEPRINT §4-5). SOLO el MoE (mlx):
    # si está frío, route_local devuelve ok=False y la MCP tool OMITE la amplificación
    # (blueprint #5: el fallback correcto NO es degradar al 8B de seguridad, es no
    # amplificar). thinking OFF obligatorio (enable_thinking=False).
    "structure_prompt": [
        Candidate("mlx", MLX_MODEL, {"temperature": 0.2, "num_predict": 900, "enable_thinking": False}),
    ],
    "critique": [
        Candidate("mlx", MLX_MODEL, {"temperature": 0.2, "num_predict": 700, "enable_thinking": False}),
    ],
    # SkillOpt REFLECT (engine/v16/skillopt.py): de fallos verificables → FLAGS baratos
    # ("qué le falta al skill"). Espejo de `critique`: SOLO MoE (mlx), fail-open si está
    # frío. El optimizador real (proponer la EDICIÓN) NO vive aquí — es Claude (canon:
    # "el crítico debe ser >= capaz que el generador"). MLX aquí solo señala, no edita.
    "skillopt_reflect": [
        Candidate("mlx", MLX_MODEL, {"temperature": 0.2, "num_predict": 500, "enable_thinking": False}),
    ],
}

# ── Fase C: ONLINE ──────────────────────────────────────────────────────────────
# Política ONLINE, FÍSICAMENTE SEPARADA de _POLICY. Solo `route()` la lee, y solo
# tras pasar el gate fail-closed. `route_local` NUNCA la ve → ninguna tarea
# periférica local puede llegar a un tercero por error.
_ONLINE_POLICY: dict[str, list[Candidate]] = {
    "outside_view": [
        Candidate("grok", "grok-4-1-fast", {"temperature": 0.4}),
    ],
}

# Marcadores que vuelven un contenido NO elegible para online (fail-closed). Espeja
# secretos (redact.py) y PHI (phi_guard.py): ante CUALQUIER coincidencia, se queda
# local. Conservador a propósito — preferimos un falso "no elegible" a una fuga.
_SENSITIVE_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|AIza[A-Za-z0-9_\-]{35}|eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\."
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[A-Za-z0-9]{20,}|xai-[A-Za-z0-9]{20,}"
    r"|(api[_-]?key|secret|password|token)\s*[=:]\s*\S{8,}"
    r"|\bssn\b|\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b|\bpatient[-_ ]?(id|name|record|history)\b"
    r"|\bpaciente\b|\bdate[-_ ]?of[-_ ]?birth\b|\bfecha[-_ ]?de[-_ ]?nacimiento\b"
    r"|\bmedical[-_ ]?record\b|\bhistoria[-_ ]?cl[ií]nica\b|\bdiagn[oó]stico\b|\bdiagnosis\b)",
    re.IGNORECASE,
)

# Cache en proceso de modelos vivos: {backend: (timestamp, set(model_names))}.
# El MCP server es un demonio de larga vida, así que la cache sobrevive entre
# llamadas. TTL corto para no martillar Ollama ni quedar pegado a un estado viejo.
_model_cache: dict[str, tuple[float, set[str]]] = {}


def _query_mac_models() -> set[str]:
    """Modelos vivos en el Ollama del Mac (curl a /api/tags). set() si está caído."""
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "3", f"{OLLAMA_MAC_URL}/api/tags"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(r.stdout)
        return {m["name"] for m in data.get("models", [])}
    except Exception:
        return set()


def _query_w2_models() -> set[str]:
    """Modelos vivos en el Ollama de W2 (ssh+curl). set() si W2 off/no responde."""
    if not W2_ENABLED:
        return set()
    try:
        r = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10",
             W2_SSH, "curl", "-s", "--max-time", "3", f"{OLLAMA_W2_URL}/api/tags"],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(r.stdout)
        return {m["name"] for m in data.get("models", [])}
    except Exception:
        return set()


def _query_mlx_models() -> set[str]:
    """¿Está vivo el mlx_lm.server local? (curl a /v1/models). set() si NO corre.

    LAZY: si el server del MoE no está arrancado, devuelve set() → el router salta el
    candidato mlx y cae a Foundation-Sec/W2 (fail-open). El MoE solo cuenta como 'vivo'
    cuando el server corre (y ocupa RAM). Devuelve {MLX_MODEL} ante cualquier respuesta
    válida (el server sirve un solo modelo) → match exacto con el candidato del _POLICY.
    """
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "3", f"{MLX_URL}/v1/models"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            json.loads(r.stdout)  # respuesta JSON válida = server arriba
            return {MLX_MODEL}
    except Exception:
        pass
    return set()


def _live_models(backend: str) -> set[str]:
    """Modelos vivos en `backend`, cacheados ~`_CACHE_TTL`s. Nunca lanza."""
    now = time.time()
    cached = _model_cache.get(backend)
    if cached:
        ts, models = cached
        # TTL corto si el último resultado vino vacío (probable blip, no realidad).
        ttl = _CACHE_TTL if models else _CACHE_TTL_EMPTY
        if (now - ts) < ttl:
            return models
    if backend == "mac":
        models = _query_mac_models()
    elif backend == "w2":
        models = _query_w2_models()
    elif backend == "mlx":
        models = _query_mlx_models()
    else:
        models = set()
    _model_cache[backend] = (now, models)
    return models


def _dispatch(cand: Candidate, prompt: str, system: str, timeout: int,
              score_out: Optional[dict] = None) -> Optional[str]:
    """Invoca el transporte correcto para el backend del candidato.

    ``score_out`` (opt-in) solo aplica al backend mlx: recibe el promise_score del cuerpo.
    """
    if cand.backend == "mac":
        return dispatch_local(prompt, model=cand.model, system=system,
                              timeout=timeout, options=cand.options or None)
    if cand.backend == "w2":
        return dispatch_w2(prompt, model=cand.model, system=system,
                           timeout=timeout, options=cand.options or None)
    if cand.backend == "mlx":
        return dispatch_mlx(prompt, model=cand.model, system=system,
                            timeout=timeout, options=cand.options or None, score_out=score_out)
    return None  # backend desconocido / no-local: nunca se ejecuta desde route_local


def _log_route_event(event: dict) -> None:
    """Append best-effort del evento `model_route` a logs/v16.1-events.jsonl."""
    try:
        lf = ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"
        if lf.parent.exists():
            with lf.open("a") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def route_local(
    task: str,
    prompt: str,
    *,
    system: str = "",
    timeout: int = 60,
    sensitive: bool = False,
    client: str = "",
    want_score: bool = False,
) -> RouteResult:
    """Enruta una tarea PERIFÉRICA a un modelo LOCAL vivo, con fallback en cascada.

    Recorre los candidatos de la política para `task` (o `default`), salta los
    que no estén vivos (health-aware) o cuyo backend no sea local (invariante de
    privacidad), e invoca el primero que responda. Fail-open: si nada responde,
    devuelve RouteResult(ok=False, text=None) — NUNCA lanza.

    Args:
        task: Clave de política ("dialectic", "default", …). Desconocida → "default".
        prompt: Texto a enviar al modelo.
        system: Prompt de sistema opcional.
        timeout: Timeout por intento, en segundos.
        sensitive: Marca de datos sensibles. Aquí es redundante (todo es local),
            pero se registra para auditoría y blinda la intención de cara a Fase C.
        client: Cliente activo, solo para telemetría.

    Returns:
        RouteResult con el texto del primer modelo que respondió (o ok=False).
    """
    t0 = time.perf_counter()
    candidates = _POLICY.get(task) or _POLICY["default"]
    tried = 0

    for idx, cand in enumerate(candidates):
        # Invariante de privacidad: route_local jamás toca un backend no-local.
        if cand.backend not in _LOCAL_BACKENDS:
            continue
        # Health-awareness: no intentes un modelo que no está vivo (absorbe el
        # drift config↔realidad y un Ollama/W2 caído sin pagar el timeout).
        if cand.model not in _live_models(cand.backend):
            continue
        tried += 1
        score_out: Optional[dict] = {} if want_score else None
        text = _dispatch(cand, prompt, system, timeout, score_out)
        if text:
            # fallback_used = el ganador no fue el candidato de máxima prioridad
            # (algo antes se saltó por health/privacidad o falló al responder).
            res = RouteResult(
                text=text, backend=cand.backend, model=cand.model, ok=True,
                fallback_used=idx > 0, latency_ms=int((time.perf_counter() - t0) * 1000),
                candidates_tried=tried,
                promise_score=(score_out or {}).get("promise_score"),
            )
            _log_route_event({
                "ts": datetime.now(UTC).isoformat(),
                "event": "model_route", "task": task, "backend": res.backend,
                "model": res.model, "ok": True, "fallback_used": res.fallback_used,
                "latency_ms": res.latency_ms, "candidates_tried": tried,
                "sensitive": bool(sensitive), "client": client,
            })
            return res

    # Nada respondió (o no hay candidato vivo): fail-open.
    res = RouteResult(
        text=None, backend="", model="", ok=False, fallback_used=False,
        latency_ms=int((time.perf_counter() - t0) * 1000),
        candidates_tried=tried, error="no_local_model_available",
    )
    _log_route_event({
        "ts": datetime.now(UTC).isoformat(),
        "event": "model_route", "task": task, "backend": "", "model": "",
        "ok": False, "fallback_used": False, "latency_ms": res.latency_ms,
        "candidates_tried": tried, "sensitive": bool(sensitive), "client": client,
        "error": res.error,
    })
    return res


# ── Fase C: gate ONLINE fail-closed ─────────────────────────────────────────────

def _online_eligible(prompt: str, system: str, client: str) -> tuple[bool, str]:
    """¿El contenido puede SALIR a un tercero? Fail-closed. Devuelve (ok, razón).

    NO elegible si: hay contexto de cliente (sus datos no van a terceros) o el
    texto contiene marcadores de secreto/PHI. Ante la mínima duda, NO elegible.
    """
    if client:
        return False, "client_context"
    if _SENSITIVE_RE.search(f"{system}\n{prompt}"):
        return False, "sensitive_marker"
    return True, ""


def _dispatch_online(cand: Candidate, prompt: str, system: str, timeout: int) -> Optional[str]:
    """Transporte para backends ONLINE. Aislado de `_dispatch` (local) a propósito."""
    if cand.backend == "grok":
        return dispatch_grok(prompt, model=cand.model, system=system,
                             timeout=timeout, options=cand.options or None)
    return None


def route(
    task: str,
    prompt: str,
    *,
    system: str = "",
    timeout: int = 60,
    allow_online: bool = False,
    sensitive: bool = False,
    client: str = "",
) -> RouteResult:
    """Entry con gate de privacidad FAIL-CLOSED. Online SOLO si está explícitamente
    permitido (`allow_online=True`), no marcado `sensitive`, sin contexto de cliente
    y sin marcadores de secreto/PHI en el texto. En cualquier otro caso —o si el
    online no responde— delega en `route_local` (100% local).

    Una tarea NUNCA sale a un tercero por defecto: `allow_online` es opt-in. Y como
    los backends online viven en `_ONLINE_POLICY` (separada de `_POLICY`), ninguna
    ruta local puede alcanzarlos. Este es el único punto del sistema que puede
    egresar contenido — y lo hace bajo triple condición.

    Args:
        task: Clave de `_ONLINE_POLICY` para el intento online; el fallback local usa `_POLICY`.
        prompt: Texto a enviar.
        system: Prompt de sistema opcional (también se escanea por sensibilidad).
        timeout: Timeout por intento (s).
        allow_online: Opt-in explícito para permitir un backend online.
        sensitive: Marca dura del caller: si True, jamás online.
        client: Cliente activo; si hay cliente, jamás online (fail-closed).

    Returns:
        RouteResult del online si procedió y respondió; si no, el de `route_local`.
    """
    if allow_online and not sensitive:
        eligible, reason = _online_eligible(prompt, system, client)
        if eligible:
            t0 = time.perf_counter()
            tried = 0
            for cand in _ONLINE_POLICY.get(task, []):
                if cand.backend not in _ONLINE_BACKENDS:
                    continue  # defensivo: _ONLINE_POLICY solo debe traer online
                tried += 1
                text = _dispatch_online(cand, prompt, system, timeout)
                if text:
                    res = RouteResult(
                        text=text, backend=cand.backend, model=cand.model, ok=True,
                        fallback_used=False, latency_ms=int((time.perf_counter() - t0) * 1000),
                        candidates_tried=tried,
                    )
                    _log_route_event({
                        "ts": datetime.now(UTC).isoformat(),
                        "event": "model_route", "task": task, "backend": res.backend,
                        "model": res.model, "ok": True, "online": True,
                        "latency_ms": res.latency_ms, "candidates_tried": tried,
                        "client": client,
                    })
                    return res
            # online permitido y elegible pero no respondió → cae a local (fail-open).
        else:
            _log_route_event({
                "ts": datetime.now(UTC).isoformat(),
                "event": "model_route", "task": task, "online_denied": True,
                "reason": reason, "client": client,
            })
    return route_local(task, prompt, system=system, timeout=timeout,
                       sensitive=sensitive, client=client)
