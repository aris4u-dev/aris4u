"""Teoría de colas — modelo formal de la CONGESTIÓN del orquestador (§8.4 del blueprint).

⚠️ NO CABLEADO A DECISIONES VIVAS — DIFERIDO, CONSERVAR (no es dead code; decisión el usuario
2026-06-24, audit roturas-ocultas). Biblioteca matemática pura (funciones cerradas, sin
estado, sin I/O). Modela el sistema que EJECUTA la búsqueda de agentes: cuántos servidores
(cap de concurrencia), cuándo satura, dónde está el cuello de botella de un pipeline.
Requiere parametrización empírica (λ, μ medidos de sessions.db) ANTES de informar cualquier
decisión real — ver architecture/LOCAL_AMPLIFIER_BLUEPRINT.md §8.4-8.5.

Mapeo a ARIS4U:
    - M/M/s        → cap de concurrencia de agentes (s servidores); ρ<1 = estable.
    - M/M/s/K      → cola finita; P(bloqueo) = guardrail anti-saturación calculado.
    - M/G/1 (P-K)  → tiempos de servicio NO exponenciales (la realidad: lognormal).
    - tándem       → pipeline review→verify→synthesize; el cuello = etapa de menor μ.
    - Jackson      → red con loops de retry (verify→review); resuelve el tráfico real.

Todas las tasas en las MISMAS unidades de tiempo (p. ej. tareas/segundo).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = [
    "QueueMetrics",
    "erlang_c",
    "mms_metrics",
    "mm1_metrics",
    "mmsk_metrics",
    "mg1_metrics",
    "TandemMetrics",
    "tandem_metrics",
    "jackson_open",
]


@dataclass(frozen=True)
class QueueMetrics:
    """Métricas de estado estacionario de un sistema de colas.

    Attributes:
        rho: Utilización por servidor (λ / (s·μ)). Estable solo si rho < 1.
        p0: Probabilidad de sistema vacío.
        p_wait: Probabilidad de que una llegada tenga que esperar (Erlang C).
        lq: Número medio de clientes EN COLA.
        num_system: Número medio de clientes EN EL SISTEMA (cola + servicio), la "L" clásica.
        wq: Tiempo medio de espera EN COLA.
        w: Tiempo medio EN EL SISTEMA.
        p_block: Probabilidad de bloqueo/rechazo (0 si la cola es infinita).
        lambda_eff: Tasa de llegada efectiva (λ·(1−p_block)).
    """

    rho: float
    p0: float
    p_wait: float
    lq: float
    num_system: float
    wq: float
    w: float
    p_block: float = 0.0
    lambda_eff: float = 0.0


def erlang_c(s: int, a: float) -> float:
    """Fórmula Erlang C: probabilidad de que una llegada espere en M/M/s.

    Args:
        s: Número de servidores (≥1).
        a: Carga ofrecida en Erlangs (λ/μ).

    Returns:
        Probabilidad de espera P(W>0). 1.0 si el sistema es inestable (a ≥ s).

    Raises:
        ValueError: Si s < 1 o a < 0.
    """
    if s < 1:
        raise ValueError("s debe ser ≥ 1")
    if a < 0:
        raise ValueError("a debe ser ≥ 0")
    rho = a / s
    if rho >= 1.0:
        return 1.0
    # term_s = a^s / s! · 1/(1-rho)
    sum_terms = sum(a**n / math.factorial(n) for n in range(s))
    term_s = (a**s / math.factorial(s)) / (1.0 - rho)
    return term_s / (sum_terms + term_s)


def mms_metrics(lam: float, mu: float, s: int) -> QueueMetrics:
    """Métricas de un sistema M/M/s (s servidores, cola infinita).

    Args:
        lam: Tasa de llegada λ.
        mu: Tasa de servicio por servidor μ.
        s: Número de servidores.

    Returns:
        QueueMetrics con el estado estacionario.

    Raises:
        ValueError: Si los parámetros son no positivos o el sistema es inestable (ρ≥1).
    """
    if lam <= 0 or mu <= 0:
        raise ValueError("lam y mu deben ser > 0")
    if s < 1:
        raise ValueError("s debe ser ≥ 1")
    a = lam / mu
    rho = a / s
    if rho >= 1.0:
        raise ValueError(
            f"sistema inestable: ρ={rho:.3f} ≥ 1 (λ={lam}, μ={mu}, s={s}). "
            "Aumenta s o reduce λ."
        )
    sum_terms = sum(a**n / math.factorial(n) for n in range(s))
    term_s = (a**s / math.factorial(s)) / (1.0 - rho)
    p0 = 1.0 / (sum_terms + term_s)
    p_wait = term_s * p0
    lq = p_wait * rho / (1.0 - rho)
    wq = lq / lam
    w = wq + 1.0 / mu
    ll = lq + a
    return QueueMetrics(
        rho=rho, p0=p0, p_wait=p_wait, lq=lq, num_system=ll, wq=wq, w=w,
        p_block=0.0, lambda_eff=lam,
    )


def mm1_metrics(lam: float, mu: float) -> QueueMetrics:
    """Caso especial M/M/1 (un servidor)."""
    return mms_metrics(lam, mu, 1)


def _mmsk_steady_state_probs(a: float, s: int, k: int) -> list[float]:
    """Distribución estacionaria normalizada [p_0 … p_k] de un M/M/s/K.

    Args:
        a: Carga ofrecida λ/μ.
        s: Número de servidores.
        k: Capacidad total.

    Returns:
        Lista de k+1 probabilidades que suman 1.
    """
    unnorm = []
    for n in range(k + 1):
        if n <= s:
            unnorm.append(a**n / math.factorial(n))
        else:
            unnorm.append(a**n / (math.factorial(s) * s ** (n - s)))
    total = sum(unnorm)
    return [u / total for u in unnorm]


def mmsk_metrics(lam: float, mu: float, s: int, k: int) -> QueueMetrics:
    """Métricas de M/M/s/K — cola FINITA con capacidad total K (incluye en servicio).

    Llegadas que encuentran el sistema lleno (K clientes) son RECHAZADAS. Esto modela
    el guardrail anti-saturación del orquestador: P(bloqueo) = probabilidad de descartar
    una tarea por sistema saturado. Estable para cualquier ρ (la finitud lo garantiza).

    Args:
        lam: Tasa de llegada λ.
        mu: Tasa de servicio por servidor μ.
        s: Número de servidores.
        k: Capacidad total del sistema (k ≥ s).

    Returns:
        QueueMetrics con p_block y lambda_eff poblados.

    Raises:
        ValueError: Si parámetros inválidos o k < s.
    """
    if lam <= 0 or mu <= 0:
        raise ValueError("lam y mu deben ser > 0")
    if s < 1:
        raise ValueError("s debe ser ≥ 1")
    if k < s:
        raise ValueError(f"k ({k}) debe ser ≥ s ({s})")
    a = lam / mu
    p = _mmsk_steady_state_probs(a, s, k)
    p0 = p[0]
    p_block = p[k]
    lambda_eff = lam * (1.0 - p_block)
    lq = sum((n - s) * p[n] for n in range(s, k + 1))
    ll = sum(n * p[n] for n in range(k + 1))
    safe_lam = lambda_eff if lambda_eff > 0 else lam
    wq = lq / safe_lam
    w = ll / safe_lam
    rho = a / s
    p_wait = sum(p[n] for n in range(s, k + 1))
    return QueueMetrics(
        rho=rho, p0=p0, p_wait=p_wait, lq=lq, num_system=ll, wq=wq, w=w,
        p_block=p_block, lambda_eff=lambda_eff,
    )


def mg1_metrics(lam: float, mu: float, service_var: float) -> QueueMetrics:
    """Métricas de M/G/1 vía Pollaczek-Khinchine — servicio de distribución GENERAL.

    La realidad de ARIS4U: los tiempos de agente NO son exponenciales (son lognormales,
    varianza alta). P-K corrige la sobre/sub-estimación de M/M/1 usando la varianza real
    del tiempo de servicio. M/D/1 (determinista) = service_var 0.

    Args:
        lam: Tasa de llegada λ.
        mu: Tasa de servicio (1/μ = tiempo medio de servicio).
        service_var: Varianza del tiempo de servicio (σ²_S), en unidades de tiempo².

    Returns:
        QueueMetrics. p_wait = ρ (un servidor).

    Raises:
        ValueError: Si parámetros inválidos o ρ≥1.
    """
    if lam <= 0 or mu <= 0:
        raise ValueError("lam y mu deben ser > 0")
    if service_var < 0:
        raise ValueError("service_var debe ser ≥ 0")
    rho = lam / mu
    if rho >= 1.0:
        raise ValueError(f"sistema inestable: ρ={rho:.3f} ≥ 1")
    # Pollaczek-Khinchine: Lq = (λ²σ² + ρ²) / (2(1-ρ))
    lq = (lam**2 * service_var + rho**2) / (2.0 * (1.0 - rho))
    wq = lq / lam
    w = wq + 1.0 / mu
    ll = lq + rho
    return QueueMetrics(
        rho=rho, p0=1.0 - rho, p_wait=rho, lq=lq, num_system=ll, wq=wq, w=w,
        p_block=0.0, lambda_eff=lam,
    )


@dataclass(frozen=True)
class TandemMetrics:
    """Métricas de una red de colas en tándem (pipeline en serie).

    Attributes:
        stage_rho: Utilización ρ_i de cada etapa.
        stage_w: Tiempo en sistema W_i de cada etapa.
        stage_l: Número medio L_i de cada etapa.
        total_w: Tiempo total de un trabajo a través del pipeline (Σ W_i).
        total_l: Número total de trabajos en el pipeline (Σ L_i).
        bottleneck: Índice (0-based) de la etapa con mayor ρ (el cuello de botella).
        stable: True si todas las etapas tienen ρ_i < 1.
    """

    stage_rho: tuple[float, ...]
    stage_w: tuple[float, ...]
    stage_l: tuple[float, ...]
    total_w: float
    total_l: float
    bottleneck: int
    stable: bool


def tandem_metrics(lam: float, mus: list[float], servers: list[int] | None = None) -> TandemMetrics:
    """Red de colas en tándem M/M/s_i por el teorema de Burke (1956).

    Burke: la salida de una cola M/M/s en equilibrio es un proceso de Poisson con la misma
    tasa λ. ⇒ cada etapa de un pipeline en serie se analiza INDEPENDIENTE como M/M/s con
    entrada λ. El cuello de botella (mayor ρ) domina el throughput total.

    Args:
        lam: Tasa de llegada externa al pipeline.
        mus: Tasa de servicio μ_i de cada etapa (una entrada por etapa).
        servers: Servidores por etapa s_i (default: 1 por etapa).

    Returns:
        TandemMetrics. Si alguna etapa es inestable, stable=False y se calcula lo posible
        (la(s) etapa(s) inestable(s) reportan W/L = inf).

    Raises:
        ValueError: Si mus vacío, lam≤0, o longitudes no coinciden.
    """
    if lam <= 0:
        raise ValueError("lam debe ser > 0")
    if not mus:
        raise ValueError("mus no puede estar vacío")
    if servers is None:
        servers = [1] * len(mus)
    if len(servers) != len(mus):
        raise ValueError("servers y mus deben tener la misma longitud")

    rhos: list[float] = []
    ws: list[float] = []
    ls: list[float] = []
    stable = True
    for mu, s in zip(mus, servers):
        if mu <= 0:
            raise ValueError("cada μ debe ser > 0")
        rho = lam / (s * mu)
        rhos.append(rho)
        if rho >= 1.0:
            stable = False
            ws.append(math.inf)
            ls.append(math.inf)
        else:
            m = mms_metrics(lam, mu, s)
            ws.append(m.w)
            ls.append(m.num_system)
    total_w = sum(ws)
    total_l = sum(ls)
    bottleneck = int(np.argmax(rhos))
    return TandemMetrics(
        stage_rho=tuple(rhos), stage_w=tuple(ws), stage_l=tuple(ls),
        total_w=total_w, total_l=total_l, bottleneck=bottleneck, stable=stable,
    )


def jackson_open(lam_ext: np.ndarray, routing: np.ndarray, mus: np.ndarray) -> np.ndarray:
    """Red de Jackson ABIERTA — resuelve las ecuaciones de tráfico con loops/realimentación.

    Modela pipelines con re-trabajo (p. ej. verify rechaza → vuelve a review). Resuelve la
    tasa de llegada efectiva a cada nodo: λ = (I − Rᵀ)⁻¹ · λ_ext, donde routing[i][j] =
    probabilidad de ir del nodo i al j tras el servicio.

    Args:
        lam_ext: Vector de llegadas externas a cada nodo (longitud n).
        routing: Matriz n×n de probabilidades de enrutamiento (filas suman ≤ 1; el resto
            sale de la red).
        mus: Vector de tasas de servicio por nodo (para chequear estabilidad).

    Returns:
        Vector de tasas de llegada efectivas λ_i a cada nodo.

    Raises:
        ValueError: Si dimensiones no coinciden o la red es inestable (algún λ_i ≥ μ_i).
    """
    lam_ext = np.asarray(lam_ext, dtype=float)
    routing = np.asarray(routing, dtype=float)
    mus = np.asarray(mus, dtype=float)
    n = lam_ext.shape[0]
    if routing.shape != (n, n):
        raise ValueError("routing debe ser n×n con n = len(lam_ext)")
    if mus.shape[0] != n:
        raise ValueError("mus debe tener longitud n")
    if np.any(routing < 0) or np.any(routing.sum(axis=1) > 1.0 + 1e-9):
        raise ValueError("routing inválido: probabilidades < 0 o filas que suman > 1")
    lam = np.linalg.solve(np.eye(n) - routing.T, lam_ext)
    if np.any(lam >= mus):
        bad = np.where(lam >= mus)[0].tolist()
        raise ValueError(f"red inestable: nodos {bad} tienen λ_i ≥ μ_i")
    return lam
