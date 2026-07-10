"""Markov Task-Outcome Advisor — asesor DISCIPLINADO sobre markov.py.

Núcleo bulletproof (como ``capacity_advisor`` sobre ``queueing``): dado un conteo
OBSERVADO de transiciones de tareas en forma canónica (estados transitorios primero,
absorbentes después), estima E[pasos hasta absorción] y P(absorción en cada estado).
Pero REHÚSA si los datos son escasos (n<min_obs por estado), y marca el supuesto de
Markov (memorylessness) — nunca da un número que no deberías creer.

No reimplementa la matemática: usa ``markov.absorbing_analysis`` (ya testeado).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from . import markov


@dataclass(frozen=True)
class MarkovAdvice:
    """Resultado disciplinado. Si ``refused``, solo ``reason`` es válido."""

    refused: bool
    reason: str
    obs_per_transient: tuple[int, ...]
    expected_steps: tuple[float, ...] | None
    absorption_probs: tuple[tuple[float, ...], ...] | None
    caveats: tuple[str, ...]


def _validate_counts(count_matrix: npt.ArrayLike, n_transient: int) -> np.ndarray:
    """Valida y devuelve la matriz de conteos como ndarray float."""
    counts = np.asarray(count_matrix, dtype=float)
    if counts.ndim != 2 or counts.shape[0] != counts.shape[1]:
        raise ValueError("count_matrix debe ser cuadrada")
    if not 1 <= n_transient < counts.shape[0]:
        raise ValueError("n_transient debe estar en [1, n-1]")
    if np.any(counts < 0):
        raise ValueError("los conteos no pueden ser negativos")
    return counts


def _build_caveats(min_obs_seen: int, min_obs: int) -> tuple[str, ...]:
    """Supuestos/advertencias del estimador de Markov."""
    caveats = [
        "asume propiedad de Markov (sin memoria): verifica que el desenlace NO dependa "
        "de la historia previa de la tarea, solo del estado actual",
        "estimación puntual (MLE): para grado-decisión falta banda de confianza "
        "(bootstrap/Dirichlet sobre los conteos)",
    ]
    if min_obs_seen < 2 * min_obs:
        caveats.append(
            f"datos apenas suficientes (min {min_obs_seen} obs/estado): alta varianza"
        )
    return tuple(caveats)


def advise_task_outcome(
    count_matrix: npt.ArrayLike,
    n_transient: int,
    *,
    min_obs: int = 30,
) -> MarkovAdvice:
    """Aconseja desenlace de tareas desde conteos OBSERVADos de transiciones.

    Args:
        count_matrix: Conteos n×n en forma canónica (transitorios [0..n_transient), luego
            absorbentes). Solo se usan las filas transitorias.
        n_transient: Nº de estados transitorios (1 <= n_transient < n).
        min_obs: Mínimo de observaciones por estado transitorio o REHÚSA. Default 30.

    Returns:
        MarkovAdvice. refused=True (con reason) si algún estado transitorio tiene < min_obs.

    Raises:
        ValueError: Si la matriz o n_transient son inválidos.
    """
    counts = _validate_counts(count_matrix, n_transient)
    trans_rows = counts[:n_transient]
    obs = trans_rows.sum(axis=1)
    obs_tuple = tuple(int(x) for x in obs)

    sparse = [i for i, o in enumerate(obs) if o < min_obs]
    if sparse:
        return MarkovAdvice(
            refused=True,
            reason=(
                f"estados transitorios {sparse} con < {min_obs} observaciones. "
                "REHÚSO estimar; junta más datos de transición primero."
            ),
            obs_per_transient=obs_tuple,
            expected_steps=None,
            absorption_probs=None,
            caveats=(),
        )

    probs = trans_rows / obs[:, None]  # MLE: fila normalizada por su total
    analysis = markov.absorbing_analysis(probs[:, :n_transient], probs[:, n_transient:])
    return MarkovAdvice(
        refused=False,
        reason="",
        obs_per_transient=obs_tuple,
        expected_steps=tuple(float(x) for x in analysis.steps_to_absorption),
        absorption_probs=tuple(
            tuple(float(x) for x in row) for row in analysis.absorption_probs
        ),
        caveats=_build_caveats(int(obs.min()), min_obs),
    )


def format_report(adv: MarkovAdvice) -> str:
    """Reporte de texto disciplinado (observaciones / respuesta / supuestos)."""
    if adv.refused:
        return f"[REHÚSO] {adv.reason}"
    lines = [
        f"[MEDIDO] obs/estado transitorio = {adv.obs_per_transient}",
        "[RESPUESTA]",
    ]
    assert adv.expected_steps is not None and adv.absorption_probs is not None
    for i, steps in enumerate(adv.expected_steps):
        probs = " · ".join(f"{p:.0%}" for p in adv.absorption_probs[i])
        lines.append(f"   desde t{i}: E[pasos]={steps:.2f} · P(absorción)=[{probs}]")
    lines.append("[SUPUESTOS]")
    lines.extend(f"   - {c}" for c in adv.caveats)
    return "\n".join(lines)
