"""Teoría de la decisión + programación dinámica — la capa de decisión formal (§8.4d).

⚠️ NO CABLEADO A DECISIONES VIVAS — DIFERIDO, CONSERVAR (no es dead code; decisión el usuario
2026-06-24, audit roturas-ocultas). Biblioteca matemática pura. Formaliza "cuánto vale
investigar antes de decidir" (VEIP/EVPI ↔ Regla #1 RESEARCH-FIRST con presupuesto) y la
decisión multi-etapa óptima (Bellman ↔ optimal-stopping del §8.3). Requiere parámetros
reales (payoffs, probabilidades) antes de informar decisiones; sin datos = teatro.

Mapeo a ARIS4U:
    - EVPI               → tokens máximos que vale gastar investigando antes de decidir.
    - maximin/Hurwicz    → dial de risk-appetite del paralelismo (worst-case del M5).
    - savage (regret)    → minimizar el arrepentimiento de elegir mal una rama.
    - bellman (DP)       → secuencia óptima de etapas de un workflow (backward induction).

Convención de matrices de pago: filas = acciones, columnas = estados de la naturaleza.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

__all__ = [
    "expected_value",
    "evpi",
    "maximin",
    "maximax",
    "hurwicz",
    "savage_minimax_regret",
    "laplace",
    "DPResult",
    "dynamic_program",
]


def _as_matrix(payoffs: Sequence[Sequence[float]]) -> np.ndarray:
    """Convierte payoffs a matriz 2D validada (acciones × estados)."""
    m = np.asarray(payoffs, dtype=float)
    if m.ndim != 2 or m.size == 0:
        raise ValueError("payoffs debe ser una matriz 2D no vacía (acciones × estados)")
    return m


def expected_value(payoffs: Sequence[Sequence[float]], probs: Sequence[float]) -> np.ndarray:
    """Valor monetario esperado (VME) de cada acción.

    Args:
        payoffs: Matriz acciones × estados.
        probs: Probabilidades de cada estado (suman 1).

    Returns:
        Vector de VME por acción.

    Raises:
        ValueError: Si dimensiones no coinciden o las probabilidades no suman 1.
    """
    m = _as_matrix(payoffs)
    p = np.asarray(probs, dtype=float)
    if p.shape[0] != m.shape[1]:
        raise ValueError("probs debe tener una entrada por estado (columna)")
    if not np.isclose(p.sum(), 1.0):
        raise ValueError("probs debe sumar 1")
    return m @ p


def evpi(payoffs: Sequence[Sequence[float]], probs: Sequence[float]) -> float:
    """Valor Esperado de la Información Perfecta (EVPI = VEIP).

    EVPI = E[pago con información perfecta] − max VME. Es el máximo que vale la pena pagar
    por eliminar la incertidumbre ANTES de decidir → en ARIS4U: el techo de tokens que
    vale gastar investigando. Si EVPI < coste(research), no investigues.

    Args:
        payoffs: Matriz acciones × estados (pagos; mayor = mejor).
        probs: Probabilidades de cada estado.

    Returns:
        EVPI ≥ 0.
    """
    m = _as_matrix(payoffs)
    p = np.asarray(probs, dtype=float)
    if p.shape[0] != m.shape[1]:
        raise ValueError("probs debe tener una entrada por estado (columna)")
    if not np.isclose(p.sum(), 1.0):
        raise ValueError("probs debe sumar 1")
    ev_perfect = float((m.max(axis=0) * p).sum())
    best_ev = float((m @ p).max())
    return max(0.0, ev_perfect - best_ev)


def maximin(payoffs: Sequence[Sequence[float]]) -> int:
    """Criterio de Wald (pesimista): acción que maximiza el peor caso.

    Returns:
        Índice de la acción óptima.
    """
    m = _as_matrix(payoffs)
    return int(np.argmax(m.min(axis=1)))


def maximax(payoffs: Sequence[Sequence[float]]) -> int:
    """Criterio optimista: acción que maximiza el mejor caso.

    Returns:
        Índice de la acción óptima.
    """
    m = _as_matrix(payoffs)
    return int(np.argmax(m.max(axis=1)))


def hurwicz(payoffs: Sequence[Sequence[float]], alpha: float) -> int:
    """Criterio de Hurwicz: combinación de optimismo (α) y pesimismo (1−α).

    k(acción) = α·max + (1−α)·min. α = coeficiente de optimismo ∈ [0,1].

    Args:
        payoffs: Matriz acciones × estados.
        alpha: Coeficiente de optimismo (0 = Wald puro, 1 = maximax).

    Returns:
        Índice de la acción óptima.

    Raises:
        ValueError: Si alpha ∉ [0,1].
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError("alpha debe estar en [0, 1]")
    m = _as_matrix(payoffs)
    scores = alpha * m.max(axis=1) + (1.0 - alpha) * m.min(axis=1)
    return int(np.argmax(scores))


def savage_minimax_regret(payoffs: Sequence[Sequence[float]]) -> int:
    """Criterio de Savage: minimiza el máximo arrepentimiento (regret).

    Regret[i][j] = max_k payoff[k][j] − payoff[i][j]. Se elige la acción cuyo máximo
    regret es menor.

    Returns:
        Índice de la acción óptima.
    """
    m = _as_matrix(payoffs)
    regret = m.max(axis=0) - m
    return int(np.argmin(regret.max(axis=1)))


def laplace(payoffs: Sequence[Sequence[float]]) -> int:
    """Criterio de Laplace (razón insuficiente): maximiza el pago medio (estados equiprobables).

    Returns:
        Índice de la acción óptima.
    """
    m = _as_matrix(payoffs)
    return int(np.argmax(m.mean(axis=1)))


@dataclass(frozen=True)
class DPResult:
    """Resultado de una programación dinámica por etapas.

    Attributes:
        value: Coste (o valor) óptimo total de inicio a fin.
        policy: Lista de estados óptimos visitados, de la etapa inicial a la final.
    """

    value: float
    policy: list


def dynamic_program(
    stages: Sequence[Sequence],
    cost_fn: Callable[[object, object, int], float],
    *,
    minimize: bool = True,
) -> DPResult:
    """Programación dinámica por backward induction sobre un DAG por etapas (Bellman).

    Resuelve el camino óptimo a través de etapas consecutivas: cada estado de la etapa i
    se conecta con todos los de la etapa i+1 vía cost_fn. Aplica el principio de
    optimalidad (la cola de un camino óptimo es óptima) → caché implícita de subdecisiones.

    Args:
        stages: Lista de etapas; cada etapa es una lista de estados. La primera y la
            última pueden tener un solo estado (origen/destino) o varios.
        cost_fn: cost_fn(estado_i, estado_j, i) = coste de ir del estado_i (etapa i) al
            estado_j (etapa i+1).
        minimize: True para minimizar el coste total; False para maximizar el valor.

    Returns:
        DPResult con el valor óptimo y la política (secuencia de estados).

    Raises:
        ValueError: Si hay menos de 2 etapas o alguna etapa está vacía.
    """
    if len(stages) < 2:
        raise ValueError("se requieren al menos 2 etapas")
    if any(len(stage) == 0 for stage in stages):
        raise ValueError("ninguna etapa puede estar vacía")

    n = len(stages)
    # Backward induction: ctg[si] = coste óptimo desde el estado si hasta el final;
    # nxt_choice[i][si] = índice del estado óptimo de la etapa i+1.
    nxt_choice: list[dict[int, int]] = [{} for _ in range(n)]
    ctg: dict[int, float] = {idx: 0.0 for idx in range(len(stages[-1]))}
    for i in range(n - 2, -1, -1):
        ctg, nxt_choice[i] = _stage_costs(stages, cost_fn, i, ctg, minimize)

    start = _arg_best([ctg[si] for si in range(len(stages[0]))], minimize)
    policy = [stages[0][start]]
    cur = start
    for i in range(n - 1):
        cur = nxt_choice[i][cur]
        policy.append(stages[i + 1][cur])
    return DPResult(value=ctg[start], policy=policy)


def _arg_best(values: list[float], minimize: bool) -> int:
    """Índice del valor óptimo (mínimo si minimize, máximo en caso contrario)."""
    best_i = 0
    for i, v in enumerate(values):
        if (v < values[best_i]) if minimize else (v > values[best_i]):
            best_i = i
    return best_i


def _stage_costs(
    stages: Sequence[Sequence],
    cost_fn: Callable[[object, object, int], float],
    i: int,
    ctg: dict[int, float],
    minimize: bool,
) -> tuple[dict[int, float], dict[int, int]]:
    """Coste óptimo y elección por estado de la etapa i (una iteración de backward induction)."""
    new_ctg: dict[int, float] = {}
    choice: dict[int, int] = {}
    for si, state in enumerate(stages[i]):
        costs = [cost_fn(state, nxt, i) + ctg[sj] for sj, nxt in enumerate(stages[i + 1])]
        best_j = _arg_best(costs, minimize)
        new_ctg[si] = costs[best_j]
        choice[si] = best_j
    return new_ctg, choice
