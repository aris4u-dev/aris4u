"""Tests de engine/v16/orchestration/calibration.py (gate del sensor local, §8.5)."""

from __future__ import annotations

import numpy as np
import pytest

from engine.v16.orchestration import calibration as c


def _signal_data(seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Score que SÍ separa: positivos centrados en 3, negativos en 0."""
    rng = np.random.default_rng(seed)
    score = np.concatenate([rng.normal(0.0, 1.0, 100), rng.normal(3.0, 1.0, 100)])
    y = np.concatenate([np.zeros(100), np.ones(100)])
    return score, y


class TestLogisticFit:
    def test_recovers_positive_slope(self) -> None:
        score, y = _signal_data()
        fit = c.logistic_fit(score, y)
        assert fit.converged
        assert fit.coef[1] > 0  # pendiente positiva del score
        assert fit.odds_ratios[1] > 1.0
        assert fit.p_values[1] < 0.05

    def test_non_binary_raises(self) -> None:
        with pytest.raises(ValueError, match="binario"):
            c.logistic_fit(np.array([1.0, 2.0, 3.0]), np.array([0.0, 1.0, 2.0]))

    def test_single_class_raises(self) -> None:
        with pytest.raises(ValueError, match="ambas clases"):
            c.logistic_fit(np.array([1.0, 2.0, 3.0]), np.array([1.0, 1.0, 1.0]))


class TestStableInverse:
    def test_matches_inverse_well_conditioned(self) -> None:
        m = np.array([[4.0, 1.0], [1.0, 3.0]])
        inv = c._stable_inverse(m)
        assert inv @ m == pytest.approx(np.eye(2), abs=1e-9)

    def test_singular_falls_back_to_pinv(self) -> None:
        # filas dependientes → singular → pinv (no excepción, salida finita)
        m = np.array([[1.0, 2.0], [2.0, 4.0]])
        out = c._stable_inverse(m)
        assert out.shape == (2, 2)
        assert bool(np.all(np.isfinite(out)))


class TestAUC:
    def test_perfect_separation(self) -> None:
        scores = np.array([0.0, 1.0, 2.0, 3.0])
        labels = np.array([0.0, 0.0, 1.0, 1.0])
        assert c.auc_score(scores, labels) == pytest.approx(1.0)

    def test_reversed(self) -> None:
        scores = np.array([3.0, 2.0, 1.0, 0.0])
        labels = np.array([0.0, 0.0, 1.0, 1.0])
        assert c.auc_score(scores, labels) == pytest.approx(0.0)

    def test_no_discrimination(self) -> None:
        scores = np.array([1.0, 1.0, 1.0, 1.0])
        labels = np.array([0.0, 1.0, 0.0, 1.0])
        assert c.auc_score(scores, labels) == pytest.approx(0.5)

    def test_single_class_raises(self) -> None:
        with pytest.raises(ValueError):
            c.auc_score(np.array([1.0, 2.0]), np.array([1.0, 1.0]))


class TestSensorGate:
    def test_predictive_signal(self) -> None:
        score, y = _signal_data()
        v = c.sensor_is_predictive(score, y)
        assert v.predictive
        assert v.odds_ratio > 1.0
        assert v.p_value < 0.05
        assert v.auc > 0.5
        assert "predictivo" in v.reason

    def test_null_signal_not_predictive(self) -> None:
        rng = np.random.default_rng(1)
        score = rng.normal(0.0, 1.0, 200)
        y = np.concatenate([np.zeros(100), np.ones(100)])
        v = c.sensor_is_predictive(score, y)
        assert not v.predictive
        assert "teatro" in v.reason

    def test_insufficient_samples_inconclusive(self) -> None:
        score = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        y = np.array([0.0, 0.0, 1.0, 1.0, 1.0])
        v = c.sensor_is_predictive(score, y, min_samples=30)
        assert not v.predictive
        assert "insuficientes" in v.reason
        assert v.n == 5

    def test_fail_closed_on_single_class(self) -> None:
        # 40 muestras pero todas clase 1 → no se puede ajustar → falla-cerrado
        score = np.linspace(0, 1, 40)
        y = np.ones(40)
        v = c.sensor_is_predictive(score, y, min_samples=30)
        assert not v.predictive
        assert "imposible" in v.reason
