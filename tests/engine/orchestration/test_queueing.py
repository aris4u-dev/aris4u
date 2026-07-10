"""Tests de engine/v16/orchestration/queueing.py contra valores de libro de texto."""

from __future__ import annotations

import math

import numpy as np
import pytest

from engine.v16.orchestration import queueing as q


class TestMM1:
    def test_textbook_values(self) -> None:
        # λ=0.5, μ=1 → ρ=0.5, L=1, Lq=0.5, Wq=1, W=2
        m = q.mm1_metrics(0.5, 1.0)
        assert m.rho == pytest.approx(0.5)
        assert m.num_system == pytest.approx(1.0)
        assert m.lq == pytest.approx(0.5)
        assert m.wq == pytest.approx(1.0)
        assert m.w == pytest.approx(2.0)
        assert m.p0 == pytest.approx(0.5)

    def test_unstable_raises(self) -> None:
        with pytest.raises(ValueError, match="inestable"):
            q.mm1_metrics(2.0, 1.0)

    def test_nonpositive_raises(self) -> None:
        with pytest.raises(ValueError):
            q.mm1_metrics(0.0, 1.0)


class TestMMs:
    def test_s2_textbook(self) -> None:
        # λ=1, μ=1, s=2 → a=1, ρ=0.5, P0=1/3, Pwait=1/3, Lq=1/3, L=4/3, W=4/3
        m = q.mms_metrics(1.0, 1.0, 2)
        assert m.rho == pytest.approx(0.5)
        assert m.p0 == pytest.approx(1 / 3)
        assert m.p_wait == pytest.approx(1 / 3)
        assert m.lq == pytest.approx(1 / 3)
        assert m.num_system == pytest.approx(4 / 3)
        assert m.w == pytest.approx(4 / 3)

    def test_reduces_to_mm1(self) -> None:
        a = q.mms_metrics(0.5, 1.0, 1)
        b = q.mm1_metrics(0.5, 1.0)
        assert a.lq == pytest.approx(b.lq)
        assert a.w == pytest.approx(b.w)

    def test_unstable_raises(self) -> None:
        with pytest.raises(ValueError, match="inestable"):
            q.mms_metrics(3.0, 1.0, 2)


class TestErlangC:
    def test_known(self) -> None:
        assert q.erlang_c(2, 1.0) == pytest.approx(1 / 3)
        assert q.erlang_c(1, 0.5) == pytest.approx(0.5)

    def test_unstable_returns_one(self) -> None:
        assert q.erlang_c(2, 2.0) == 1.0

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            q.erlang_c(0, 1.0)


class TestMMsK:
    def test_mm1k_formula(self) -> None:
        # M/M/1/2, ρ=0.5: p0=0.5/(1-0.5^3)=0.5714, p_block=p2=0.25*p0
        m = q.mmsk_metrics(1.0, 2.0, 1, 2)
        p0_expected = (1 - 0.5) / (1 - 0.5**3)
        assert m.p0 == pytest.approx(p0_expected, rel=1e-6)
        assert m.p_block == pytest.approx(0.25 * p0_expected, rel=1e-6)
        assert m.lambda_eff == pytest.approx(1.0 * (1 - m.p_block))
        assert 0.0 < m.p_block < 1.0

    def test_blocking_increases_when_smaller_k(self) -> None:
        small = q.mmsk_metrics(2.0, 1.0, 1, 2)
        large = q.mmsk_metrics(2.0, 1.0, 1, 8)
        assert small.p_block > large.p_block

    def test_k_less_than_s_raises(self) -> None:
        with pytest.raises(ValueError, match="k"):
            q.mmsk_metrics(1.0, 1.0, 3, 2)


class TestMG1:
    def test_md1_deterministic(self) -> None:
        # M/D/1 (var=0): Lq = ρ²/(2(1-ρ)). λ=0.5,μ=1 → ρ=0.5 → Lq=0.25
        m = q.mg1_metrics(0.5, 1.0, 0.0)
        assert m.lq == pytest.approx(0.25)

    def test_matches_mm1_with_exponential_variance(self) -> None:
        # Servicio exponencial: var = 1/μ². Debe igualar M/M/1.
        m = q.mg1_metrics(0.5, 1.0, 1.0)  # 1/μ²=1
        assert m.lq == pytest.approx(q.mm1_metrics(0.5, 1.0).lq)

    def test_unstable_raises(self) -> None:
        with pytest.raises(ValueError, match="inestable"):
            q.mg1_metrics(2.0, 1.0, 0.5)


class TestTandem:
    def test_bottleneck_and_total(self) -> None:
        # λ=1, μ=[2,4,3] → ρ=[0.5,0.25,0.333], cuello = etapa 0
        t = q.tandem_metrics(1.0, [2.0, 4.0, 3.0])
        assert t.bottleneck == 0
        assert t.stable
        assert t.stage_rho[0] == pytest.approx(0.5)
        # W total = Σ 1/(μ_i - λ) = 1 + 1/3 + 1/2 = 1.8333
        assert t.total_w == pytest.approx(1.0 + 1 / 3 + 0.5)

    def test_unstable_stage_marks_not_stable(self) -> None:
        t = q.tandem_metrics(1.0, [2.0, 0.5])  # etapa 1: ρ=2 inestable
        assert not t.stable
        assert math.isinf(t.stage_w[1])
        assert t.bottleneck == 1

    def test_empty_mus_raises(self) -> None:
        with pytest.raises(ValueError):
            q.tandem_metrics(1.0, [])


class TestJacksonOpen:
    def test_no_routing_equals_external(self) -> None:
        lam_ext = np.array([1.0, 0.0])
        routing = np.zeros((2, 2))
        mus = np.array([2.0, 2.0])
        lam = q.jackson_open(lam_ext, routing, mus)
        assert lam == pytest.approx(lam_ext)

    def test_feedback_loop_amplifies(self) -> None:
        # nodo 0 reenvía 50% a nodo 1, nodo 1 reenvía 50% de vuelta a 0
        lam_ext = np.array([1.0, 0.0])
        routing = np.array([[0.0, 0.5], [0.5, 0.0]])
        mus = np.array([10.0, 10.0])
        lam = q.jackson_open(lam_ext, routing, mus)
        # λ0 = 1 + 0.5 λ1 ; λ1 = 0.5 λ0 → λ0=4/3, λ1=2/3
        assert lam[0] == pytest.approx(4 / 3)
        assert lam[1] == pytest.approx(2 / 3)

    def test_unstable_raises(self) -> None:
        with pytest.raises(ValueError, match="inestable"):
            q.jackson_open(np.array([5.0]), np.array([[0.0]]), np.array([2.0]))
