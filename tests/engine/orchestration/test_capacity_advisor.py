"""Tests del asesor de capacidad disciplinado (núcleo bulletproof)."""

from __future__ import annotations

import random

import pytest

from engine.v16.orchestration import capacity_advisor as ca


def test_refuses_below_min_samples() -> None:
    adv = ca.advise([300.0] * 10, servers=16)
    assert adv.refused
    assert "REHÚSO" in adv.reason
    assert adv.answer_low is None


def test_regular_service_flagged_non_exponential() -> None:
    # servicio casi constante → CV≈0 → NO exponencial
    adv = ca.advise([300.0 + i * 0.1 for i in range(40)], servers=16)
    assert not adv.refused
    assert not adv.exponential_ok
    assert adv.cv_service < ca._EXP_CV_LO
    assert any("exponencial" in c for c in adv.caveats)


def test_max_load_returns_range_not_scalar() -> None:
    times = [300.0 + (i % 5) * 20 for i in range(40)]
    adv = ca.advise(times, servers=16)
    assert adv.answer_low is not None and adv.answer_high is not None
    assert adv.answer_low <= adv.answer_high
    assert adv.answer_unit == "llegadas/hora"


def test_min_servers_question() -> None:
    times = [2700.0 + (i % 7) * 60 for i in range(40)]  # ~45 min de servicio
    adv = ca.advise(times, arrival_rate_per_hour=6.0)
    assert adv.answer_unit == "servidores"
    assert adv.answer_low is not None and adv.answer_low >= 1
    assert adv.answer_high is not None and adv.answer_low <= adv.answer_high


def test_high_variance_service_no_crash_degenerate_ci() -> None:
    # CV muy alto a n=30 → el IC95% cruza cero (Bug 1: antes daba μ negativo/crash)
    times = [1.0] * 28 + [1000.0, 1000.0]
    adv = ca.advise(times, servers=4)
    assert not adv.refused
    assert adv.answer_low is not None and adv.answer_high is not None
    assert adv.answer_low <= adv.answer_high
    assert any("cruzó cero" in c for c in adv.caveats)


def test_sub_poisson_arrivals_caveat_direction() -> None:
    # llegadas regulares (CV≈0 < Poisson): M/M/s SOBRE-estima, NO es "ráfaga" (Bug 2)
    service = [300.0 + (i % 5) * 30 for i in range(40)]
    arrivals = [i * 60.0 for i in range(40)]
    adv = ca.advise(service, servers=16, arrival_times_epoch=arrivals)
    assert adv.poisson_ok is False
    joined = " ".join(adv.caveats)
    assert "regular" in joined and "sobreestima" in joined
    assert "ráfaga" not in joined


def test_infeasible_target_raises() -> None:
    # target inalcanzable para s dado (Bug 3: antes devolvía 0.0 silencioso)
    with pytest.raises(ValueError):
        ca.advise([300.0] * 40, servers=1, target_wait_prob=0.001)


def test_requires_exactly_one_question() -> None:
    with pytest.raises(ValueError):
        ca.advise([300.0] * 40)  # ni servers ni arrival_rate
    with pytest.raises(ValueError):
        ca.advise([300.0] * 40, servers=16, arrival_rate_per_hour=6.0)


def test_bad_target_rejected() -> None:
    with pytest.raises(ValueError):
        ca.advise([300.0] * 40, servers=16, target_wait_prob=1.5)


def test_poisson_unverified_without_arrival_times() -> None:
    adv = ca.advise([300.0 + (i % 5) * 30 for i in range(40)], servers=16)
    assert adv.poisson_ok is None
    assert not adv.decision_grade  # sin verificar Poisson NO es grado-decisión


def test_decision_grade_when_both_assumptions_hold() -> None:
    rng = random.Random(42)
    service = [rng.expovariate(1 / 300.0) for _ in range(500)]  # CV≈1 (exponencial)
    t = 0.0
    arrivals: list[float] = []
    for _ in range(500):
        t += rng.expovariate(1 / 60.0)  # inter-arribos exponenciales → Poisson
        arrivals.append(t)
    adv = ca.advise(service, servers=16, arrival_times_epoch=arrivals)
    assert adv.exponential_ok
    assert adv.poisson_ok is True
    assert adv.decision_grade
    assert not adv.caveats


def test_report_formats_refusal_and_answer() -> None:
    refused = ca.advise([300.0] * 5, servers=16)
    assert ca.format_report(refused).startswith("[REHÚSO]")
    ok = ca.advise([300.0 + (i % 5) * 30 for i in range(40)], servers=16)
    report = ca.format_report(ok)
    assert "[RESPUESTA]" in report and "RANGO" in report
