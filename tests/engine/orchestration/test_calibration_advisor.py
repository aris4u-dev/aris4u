"""Tests del calibration/predictive advisor (capa fina que DELEGA en calibration.py).

La estadística (logistic_fit/auc/sensor) ya está testeada en test_calibration.py;
aquí solo verificamos la delegación, el rehúso propagado y el reporte.
"""

from __future__ import annotations

from engine.v16.orchestration import calibration_advisor as ca


def test_refuses_insufficient_samples() -> None:
    v = ca.advise_predictive([1.0, 2.0, 3.0], [0.0, 1.0, 0.0])  # n=3 < 30
    assert not v.predictive
    assert "insuficientes" in v.reason
    assert v.n == 3


def test_delegates_valid_dataset() -> None:
    feature = [float(i) for i in range(40)]
    outcome = [float(i % 2) for i in range(40)]  # ambas clases, converge
    v = ca.advise_predictive(feature, outcome)
    assert v.n == 40
    assert isinstance(v.predictive, bool)


def test_format_report_includes_feature_name() -> None:
    v = ca.advise_predictive([1.0, 2.0, 3.0], [0.0, 1.0, 0.0])
    report = ca.format_report(v, feature_name="tiempo_de_espera")
    assert "tiempo_de_espera" in report
    assert "n=3" in report
