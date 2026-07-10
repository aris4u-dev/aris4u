"""Tests de engine/v16/orchestration/markov.py contra valores de libro de texto."""

from __future__ import annotations

import numpy as np
import pytest

from engine.v16.orchestration import markov as m


class TestStochastic:
    def test_valid(self) -> None:
        assert m.is_stochastic(np.array([[0.9, 0.1], [0.5, 0.5]]))

    def test_rows_not_summing_one(self) -> None:
        assert not m.is_stochastic(np.array([[0.9, 0.2], [0.5, 0.5]]))

    def test_negative(self) -> None:
        assert not m.is_stochastic(np.array([[1.1, -0.1], [0.5, 0.5]]))

    def test_non_square(self) -> None:
        assert not m.is_stochastic(np.array([[0.5, 0.5, 0.0]]))


class TestStationary:
    def test_two_state(self) -> None:
        # P=[[0.9,0.1],[0.5,0.5]] → π=[5/6, 1/6]
        p = np.array([[0.9, 0.1], [0.5, 0.5]])
        pi = m.stationary_distribution(p)
        assert pi == pytest.approx([5 / 6, 1 / 6], abs=1e-9)
        assert pi.sum() == pytest.approx(1.0)

    def test_stationary_is_invariant(self) -> None:
        p = np.array([[0.7, 0.3], [0.4, 0.6]])
        pi = m.stationary_distribution(p)
        assert pi @ p == pytest.approx(pi, abs=1e-9)

    def test_non_stochastic_raises(self) -> None:
        with pytest.raises(ValueError):
            m.stationary_distribution(np.array([[0.9, 0.2], [0.5, 0.5]]))


class TestAbsorbing:
    def test_symmetric_random_walk(self) -> None:
        # 2 transitorios que rebotan entre sí (0↔1), cada uno con 0.5 de absorber.
        q = np.array([[0.0, 0.5], [0.5, 0.0]])
        r = np.array([[0.5, 0.0], [0.0, 0.5]])
        res = m.absorbing_analysis(q, r)
        # N = (I-Q)^-1 = [[4/3, 2/3],[2/3,4/3]]; pasos = N·1 = [2, 2]
        assert res.steps_to_absorption == pytest.approx([2.0, 2.0])
        # filas de absorption_probs suman 1
        assert res.absorption_probs.sum(axis=1) == pytest.approx([1.0, 1.0])

    def test_gamblers_ruin_steps(self) -> None:
        # 3 estados internos de una caminata simple p=0.5; pasos esperados conocidos:
        # estados 1,2,3 con barreras absorbentes en 0 y 4 → E[pasos]=[3,4,3]
        q = np.array([[0.0, 0.5, 0.0], [0.5, 0.0, 0.5], [0.0, 0.5, 0.0]])
        r = np.array([[0.5, 0.0], [0.0, 0.0], [0.0, 0.5]])
        res = m.absorbing_analysis(q, r)
        assert res.steps_to_absorption == pytest.approx([3.0, 4.0, 3.0])

    def test_bad_dims_raises(self) -> None:
        with pytest.raises(ValueError):
            m.absorbing_analysis(np.array([[0.0, 0.5]]), np.array([[0.5]]))


class TestBirthDeath:
    def test_geometric_mm1(self) -> None:
        # λ const, μ const → π_k = (1-ρ)ρ^k (M/M/1 truncado)
        pi = m.birth_death_stationary(np.array([0.5] * 30), np.array([1.0] * 30))
        assert pi[0] == pytest.approx(0.5, abs=1e-3)
        assert pi[1] / pi[0] == pytest.approx(0.5)
        assert pi.sum() == pytest.approx(1.0)

    def test_mismatched_lengths_raises(self) -> None:
        with pytest.raises(ValueError):
            m.birth_death_stationary(np.array([0.5, 0.5]), np.array([1.0]))

    def test_nonpositive_mu_raises(self) -> None:
        with pytest.raises(ValueError):
            m.birth_death_stationary(np.array([0.5]), np.array([0.0]))
