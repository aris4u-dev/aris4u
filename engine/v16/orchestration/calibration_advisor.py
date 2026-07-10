"""Predictive Advisor — capa fina de entrada general + reporte sobre calibration.py.

NOTA HONESTA: a diferencia de queueing/markov/decision (matemática PURA que necesitaba
su capa de disciplina), ``calibration.py`` YA la trae: ``sensor_is_predictive`` rehúsa si
n<min_samples y falla-cerrado a "no predictivo". Reimplementar eso sería REDUNDANTE.

Este módulo por tanto NO reimplementa nada — solo:
  (a) expone un nombre GENERAL — "¿la feature X predice el resultado binario Y?" —
      delegando en calibration, para uso más allá del sensor local (KPIs de cliente:
      ¿espera predice churn?, ¿score predice default?, etc.);
  (b) un ``format_report`` consistente con los otros asesores del paquete.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from . import calibration as cal


def advise_predictive(
    feature: Sequence[float],
    outcome: Sequence[float],
    *,
    min_samples: int = 30,
    alpha: float = 0.05,
) -> cal.SensorVerdict:
    """¿La ``feature`` medida predice el ``outcome`` binario? (DELEGA en calibration).

    Delega en ``calibration.sensor_is_predictive`` — que ya trae la disciplina: rehúsa si
    n<min_samples y falla-cerrado a "no predictivo". Reencuadra para uso general (no solo
    el promise_score del cuerpo local): cualquier feature medida vs. cualquier desenlace
    binario de negocio.

    Args:
        feature: Valores medidos de la feature (n,).
        outcome: Desenlace binario real (n,), 1 = ocurrió.
        min_samples: Mínimo de muestras para un veredicto fiable. Default 30.
        alpha: Nivel de significancia (p-valor de Wald). Default 0.05.

    Returns:
        SensorVerdict (predictive / odds_ratio / p_value / auc / n / reason).
    """
    return cal.sensor_is_predictive(
        np.asarray(feature, dtype=float),
        np.asarray(outcome, dtype=float),
        min_samples=min_samples,
        alpha=alpha,
    )


def format_report(verdict: cal.SensorVerdict, feature_name: str = "feature") -> str:
    """Reporte de texto consistente con los otros asesores del paquete."""
    veredicto = "SÍ predice" if verdict.predictive else "NO predice (o inconcluso)"
    return (
        f"[PREDICTIVO] ¿'{feature_name}' predice el resultado? → {veredicto}\n"
        f"[DETALLE] n={verdict.n} · {verdict.reason}"
    )
