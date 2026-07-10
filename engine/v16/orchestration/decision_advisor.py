"""Decision Advisor — asesor DISCIPLINADO sobre decision.py.

Núcleo bulletproof (como capacity_advisor sobre queueing): dada una matriz de pagos
(acciones × estados), recomienda una acción y — con probabilidades — calcula el **EVPI**
("cuánto vale investigar más antes de decidir"; Regla #1 RESEARCH-FIRST con presupuesto).

Su disciplina: expone si los criterios **CONCUERDAN** (decisión robusta) o **DISCREPAN**
(sensible a tu actitud ante el riesgo → no hay 'correcta' sin fijar tu postura), y marca
que payoffs/probabilidades son estimados. No reimplementa la matemática (usa decision.py).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from . import decision as dec


@dataclass(frozen=True)
class DecisionAdvice:
    """Resultado disciplinado del asesor de decisión."""

    mode: str  # "risk" (con probabilidades) | "uncertainty" (sin ellas)
    recommended: int
    recommended_label: str
    expected_values: tuple[float, ...] | None
    evpi: float | None
    criteria: dict[str, int]  # criterio → índice de acción que elige
    robust: bool  # ¿todos los criterios coinciden?
    caveats: tuple[str, ...]


def _criteria_picks(payoffs: Sequence[Sequence[float]], alpha: float) -> dict[str, int]:
    """Acción que elige cada criterio no-probabilístico (+ Hurwicz con α)."""
    return {
        "maximin": dec.maximin(payoffs),
        "maximax": dec.maximax(payoffs),
        "laplace": dec.laplace(payoffs),
        "savage": dec.savage_minimax_regret(payoffs),
        f"hurwicz(a={alpha:g})": dec.hurwicz(payoffs, alpha),
    }


def _consensus(criteria: dict[str, int]) -> int:
    """Acción de consenso; en empate, la más conservadora (maximin)."""
    counts = Counter(criteria.values()).most_common()
    if len(counts) > 1 and counts[0][1] == counts[1][1]:
        return criteria["maximin"]
    return counts[0][0]


def advise_decision(
    payoffs: Sequence[Sequence[float]],
    *,
    probs: Sequence[float] | None = None,
    labels: Sequence[str] | None = None,
    alpha: float = 0.5,
) -> DecisionAdvice:
    """Aconseja una decisión bajo riesgo (con probs) o incertidumbre (sin ellas).

    Args:
        payoffs: Matriz acciones × estados (pagos; mayor = mejor).
        probs: Probabilidades de los estados; si se dan → modo "risk" (VME + EVPI).
        labels: Nombres de las acciones (opcional).
        alpha: Coeficiente de optimismo de Hurwicz ∈ [0,1]. Default 0.5.

    Returns:
        DecisionAdvice con la recomendación, criterios, robustez y (en riesgo) EVPI.

    Raises:
        ValueError: Si payoffs es inválida, labels no calza, o probs no suma 1.
    """
    m = np.asarray(payoffs, dtype=float)
    if m.ndim != 2 or m.size == 0:
        raise ValueError("payoffs debe ser 2D no vacía (acciones × estados)")
    n_actions = m.shape[0]
    names = list(labels) if labels is not None else [f"acción{i}" for i in range(n_actions)]
    if len(names) != n_actions:
        raise ValueError("labels debe tener una entrada por acción")

    criteria = _criteria_picks(payoffs, alpha)
    caveats = ["payoffs son estimados; la recomendación hereda su error"]

    if probs is not None:
        ev = dec.expected_value(payoffs, probs)  # valida probs (suma 1)
        recommended = int(np.argmax(ev))
        criteria["max-EV"] = recommended
        mode = "risk"
        expected_values: tuple[float, ...] | None = tuple(float(x) for x in ev)
        evpi_val: float | None = dec.evpi(payoffs, probs)
    else:
        recommended = _consensus(criteria)
        mode = "uncertainty"
        expected_values = None
        evpi_val = None
        caveats.append(
            "sin probabilidades = incertidumbre pura; el criterio depende de tu risk-appetite"
        )

    robust = len(set(criteria.values())) == 1
    if not robust:
        caveats.append(
            "los criterios DISCREPAN → decisión sensible a tu actitud ante el riesgo "
            "(no hay 'correcta' sin fijar tu postura)"
        )

    return DecisionAdvice(
        mode=mode,
        recommended=recommended,
        recommended_label=names[recommended],
        expected_values=expected_values,
        evpi=evpi_val,
        criteria=criteria,
        robust=robust,
        caveats=tuple(caveats),
    )


def format_report(adv: DecisionAdvice) -> str:
    """Reporte de texto disciplinado (recomendación / EVPI / robustez / supuestos)."""
    lines = [
        f"[MODO] {adv.mode} · recomendado: {adv.recommended_label} (acción {adv.recommended})"
    ]
    if adv.mode == "risk":
        assert adv.expected_values is not None and adv.evpi is not None
        vme = ", ".join(f"{v:.2f}" for v in adv.expected_values)
        lines.append(f"[VME por acción] {vme}")
        lines.append(
            f"[EVPI] {adv.evpi:.2f} — investiga más SOLO si el costo de hacerlo < EVPI"
        )
    picks = " · ".join(f"{k}→{v}" for k, v in adv.criteria.items())
    verdict = "ROBUSTO (todos concuerdan)" if adv.robust else "DISCREPAN (sensible al riesgo)"
    lines.append(f"[CRITERIOS] {picks}  ⇒ {verdict}")
    lines.append("[SUPUESTOS]")
    lines.extend(f"   - {c}" for c in adv.caveats)
    return "\n".join(lines)
