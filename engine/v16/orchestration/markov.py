"""Cadenas de Markov — estados del orquestador y la base que unifica las colas (§8.4c).

⚠️ NO CABLEADO A DECISIONES VIVAS — DIFERIDO, CONSERVAR (no es dead code; decisión el usuario
2026-06-24, audit roturas-ocultas). Biblioteca matemática pura. Modela la dinámica de
estados de una tarea (encolada→en proceso→aceptada/rechazada) y el proceso de
nacimiento-muerte que subyace a la teoría de colas. Requiere calibrar la matriz de
transición con datos reales de sessions.db antes de informar cualquier decisión.

Mapeo a ARIS4U:
    - matriz de transición P    → transiciones del depth_protocol / estados de tarea.
    - distribución estacionaria → fracción de tiempo en cada estado a largo plazo.
    - matriz fundamental (I−Q)⁻¹ → E[nº de revisiones hasta aceptar] = coste esperado.
    - prob. de absorción        → P(una tarea termine aceptada vs rechazada).
    - nacimiento-muerte         → la cola como cadena de Markov (unifica con queueing.py).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "is_stochastic",
    "stationary_distribution",
    "AbsorbingAnalysis",
    "absorbing_analysis",
    "birth_death_stationary",
]

_TOL = 1e-9


def is_stochastic(p: np.ndarray, tol: float = 1e-9) -> bool:
    """True si P es una matriz estocástica por filas (filas no negativas que suman 1).

    Args:
        p: Matriz cuadrada candidata.
        tol: Tolerancia para la suma de filas.

    Returns:
        True si P es estocástica por filas.
    """
    p = np.asarray(p, dtype=float)
    if p.ndim != 2 or p.shape[0] != p.shape[1]:
        return False
    if np.any(p < -tol):
        return False
    return bool(np.allclose(p.sum(axis=1), 1.0, atol=tol))


def stationary_distribution(p: np.ndarray) -> np.ndarray:
    """Distribución estacionaria π de una cadena de Markov (π·P = π, Σπ = 1).

    Calcula el vector propio izquierdo asociado al valor propio 1.

    Args:
        p: Matriz de transición estocástica por filas (n×n).

    Returns:
        Vector π de longitud n, no negativo, que suma 1.

    Raises:
        ValueError: Si P no es estocástica por filas.
    """
    p = np.asarray(p, dtype=float)
    if not is_stochastic(p):
        raise ValueError("P no es una matriz estocástica por filas")
    n = p.shape[0]
    # π (Pᵀ) = π  →  vector propio izquierdo de P = vector propio derecho de Pᵀ con λ=1
    eigvals, eigvecs = np.linalg.eig(p.T)
    idx = int(np.argmin(np.abs(eigvals - 1.0)))
    vec = np.real(eigvecs[:, idx])
    total = vec.sum()
    if abs(total) < _TOL:
        # Degenerado (p. ej. signos mixtos): caer a resolver el sistema lineal.
        a = np.vstack([p.T - np.eye(n), np.ones(n)])
        b = np.concatenate([np.zeros(n), [1.0]])
        vec, *_ = np.linalg.lstsq(a, b, rcond=None)
        return np.clip(vec, 0.0, None)
    pi = vec / total
    return np.clip(pi, 0.0, None)


@dataclass(frozen=True)
class AbsorbingAnalysis:
    """Análisis de una cadena de Markov absorbente en forma canónica.

    Attributes:
        fundamental: Matriz fundamental N = (I−Q)⁻¹. N[i][j] = nº esperado de visitas al
            estado transitorio j partiendo de i, antes de la absorción.
        steps_to_absorption: Vector t = N·1. t[i] = nº esperado de pasos hasta absorber
            desde el estado transitorio i (= "revisiones hasta aceptar/rechazar").
        absorption_probs: Matriz B = N·R. B[i][j] = P(absorber en el estado absorbente j
            partiendo del transitorio i).
    """

    fundamental: np.ndarray
    steps_to_absorption: np.ndarray
    absorption_probs: np.ndarray


def absorbing_analysis(q: np.ndarray, r: np.ndarray) -> AbsorbingAnalysis:
    """Resuelve una cadena de Markov absorbente dada su forma canónica.

    Una cadena absorbente se escribe P = [[Q, R], [0, I]] donde Q (t×t) son las
    transiciones entre estados transitorios y R (t×a) las de transitorio a absorbente.

    Args:
        q: Submatriz transitorio→transitorio (t×t).
        r: Submatriz transitorio→absorbente (t×a).

    Returns:
        AbsorbingAnalysis con N, t y B.

    Raises:
        ValueError: Si las dimensiones no son compatibles o (I−Q) es singular.
    """
    q = np.asarray(q, dtype=float)
    r = np.asarray(r, dtype=float)
    t = q.shape[0]
    if q.shape != (t, t):
        raise ValueError("Q debe ser cuadrada (t×t)")
    if r.shape[0] != t:
        raise ValueError("R debe tener t filas (mismas que Q)")
    try:
        fundamental = np.linalg.inv(np.eye(t) - q)
    except np.linalg.LinAlgError as exc:
        raise ValueError("(I−Q) es singular: ¿hay estados transitorios sin salida?") from exc
    steps = fundamental @ np.ones(t)
    absorption = fundamental @ r
    return AbsorbingAnalysis(
        fundamental=fundamental, steps_to_absorption=steps, absorption_probs=absorption,
    )


def birth_death_stationary(lambdas: np.ndarray, mus: np.ndarray) -> np.ndarray:
    """Distribución estacionaria de un proceso de nacimiento-muerte finito.

    Estados 0..n. λ_i = tasa de nacimiento en el estado i (longitud n: transiciones
    0→1, 1→2, …, (n-1)→n). μ_i = tasa de muerte desde el estado i+1 (longitud n).
    π_k = π_0 · Π_{i<k} (λ_i / μ_i). Esto demuestra que una cola M/M/s es un caso
    particular de cadena de Markov (unifica queueing.py con este módulo).

    Args:
        lambdas: Tasas de nacimiento (longitud n).
        mus: Tasas de muerte (longitud n), todas > 0.

    Returns:
        Vector π de longitud n+1, no negativo, que suma 1.

    Raises:
        ValueError: Si longitudes no coinciden o alguna μ ≤ 0.
    """
    lambdas = np.asarray(lambdas, dtype=float)
    mus = np.asarray(mus, dtype=float)
    if lambdas.shape != mus.shape:
        raise ValueError("lambdas y mus deben tener la misma longitud")
    if np.any(mus <= 0):
        raise ValueError("todas las μ deben ser > 0")
    n = lambdas.shape[0]
    ratios = np.ones(n + 1)
    for k in range(1, n + 1):
        ratios[k] = ratios[k - 1] * (lambdas[k - 1] / mus[k - 1])
    return ratios / ratios.sum()
