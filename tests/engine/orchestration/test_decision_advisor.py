"""Tests del decision advisor (asesor disciplinado sobre decision.py)."""

from __future__ import annotations

import pytest

from engine.v16.orchestration import decision_advisor as da

PAYOFFS = [[100.0, -50.0], [40.0, 30.0]]  # 2 acciones × 2 estados


def test_risk_mode_ev_and_evpi() -> None:
    adv = da.advise_decision(PAYOFFS, probs=[0.5, 0.5])
    assert adv.mode == "risk"
    assert adv.recommended == 1  # VME = [25, 35] → gana la acción 1
    assert adv.expected_values == pytest.approx((25.0, 35.0))
    assert adv.evpi == pytest.approx(30.0)


def test_uncertainty_mode_criteria_disagree() -> None:
    adv = da.advise_decision(PAYOFFS)
    assert adv.mode == "uncertainty"
    assert not adv.robust  # maximax elige 0; el resto, 1
    assert adv.criteria["maximax"] == 0
    assert any("DISCREPAN" in c for c in adv.caveats)


def test_robust_when_one_action_dominates() -> None:
    adv = da.advise_decision([[10.0, 10.0], [1.0, 1.0]])
    assert adv.robust
    assert adv.recommended == 0


def test_bad_probs_raise() -> None:
    with pytest.raises(ValueError):
        da.advise_decision(PAYOFFS, probs=[0.5, 0.6])  # no suma 1


def test_labels_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        da.advise_decision(PAYOFFS, labels=["solo-uno"])  # hay 2 acciones


def test_format_report() -> None:
    adv = da.advise_decision(PAYOFFS, probs=[0.5, 0.5], labels=["A", "B"])
    report = da.format_report(adv)
    assert "EVPI" in report and "recomendado: B" in report
