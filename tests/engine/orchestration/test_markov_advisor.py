"""Tests del Markov task-outcome advisor (asesor disciplinado sobre markov.py)."""

from __future__ import annotations

import pytest

from engine.v16.orchestration import markov_advisor as ma


def test_refuses_sparse_data() -> None:
    adv = ma.advise_task_outcome([[0, 5, 3], [0, 0, 0], [0, 0, 0]], n_transient=1)
    assert adv.refused
    assert "REHÚSO" in adv.reason
    assert adv.expected_steps is None


def test_single_transient_exact() -> None:
    # 1 transitorio → 2 absorbentes con 60/40 → E[pasos]=1, P=[0.6,0.4]
    adv = ma.advise_task_outcome([[0, 60, 40], [0, 0, 0], [0, 0, 0]], n_transient=1)
    assert not adv.refused
    assert adv.expected_steps == pytest.approx((1.0,))
    assert adv.absorption_probs is not None
    assert adv.absorption_probs[0] == pytest.approx((0.6, 0.4))


def test_absorption_probs_sum_to_one() -> None:
    # invariante: en una cadena absorbente propia, la absorción es CIERTA (filas de B suman 1)
    counts = [
        [0, 40, 60, 0],
        [30, 0, 0, 70],
        [0, 0, 0, 0],
        [0, 0, 0, 0],
    ]
    adv = ma.advise_task_outcome(counts, n_transient=2)
    assert not adv.refused
    assert adv.absorption_probs is not None
    for row in adv.absorption_probs:
        assert sum(row) == pytest.approx(1.0)


def test_flags_markov_property_caveat() -> None:
    adv = ma.advise_task_outcome([[0, 60, 40], [0, 0, 0], [0, 0, 0]], n_transient=1)
    assert any("Markov" in c for c in adv.caveats)


def test_bad_inputs_raise() -> None:
    with pytest.raises(ValueError):
        ma.advise_task_outcome([[1, 2, 3]], n_transient=1)  # no cuadrada
    with pytest.raises(ValueError):
        ma.advise_task_outcome([[0, 1], [0, 0]], n_transient=2)  # n_transient >= n
    with pytest.raises(ValueError):
        ma.advise_task_outcome([[0, -1, 2], [0, 0, 0], [0, 0, 0]], n_transient=1)  # negativos


def test_format_report() -> None:
    refused = ma.advise_task_outcome([[0, 1], [0, 0]], n_transient=1)  # obs=1 < 30
    assert ma.format_report(refused).startswith("[REHÚSO]")
    ok = ma.advise_task_outcome([[0, 60, 40], [0, 0, 0], [0, 0, 0]], n_transient=1)
    report = ma.format_report(ok)
    assert "E[pasos]" in report and "P(absorción)" in report
