"""Tests de engine/v16/orchestration/decision.py contra valores de libro de texto."""

from __future__ import annotations

import pytest

from engine.v16.orchestration import decision as d

# Matriz de pagos de referencia: acciones × estados
PAYOFFS = [[100.0, -20.0], [40.0, 40.0]]


class TestExpectedValue:
    def test_vme(self) -> None:
        ev = d.expected_value(PAYOFFS, [0.5, 0.5])
        assert ev == pytest.approx([40.0, 40.0])

    def test_bad_probs_raises(self) -> None:
        with pytest.raises(ValueError):
            d.expected_value(PAYOFFS, [0.3, 0.3])


class TestEVPI:
    def test_textbook(self) -> None:
        # EV_perfecto = 0.5·max(100,40) + 0.5·max(-20,40) = 50+20 = 70
        # best_ev = 40 → EVPI = 30
        assert d.evpi(PAYOFFS, [0.5, 0.5]) == pytest.approx(30.0)

    def test_zero_when_dominant(self) -> None:
        # acción 1 domina en ambos estados → EVPI=0
        assert d.evpi([[10.0, 10.0], [1.0, 2.0]], [0.5, 0.5]) == pytest.approx(0.0)


class TestCriteria:
    def test_maximin(self) -> None:
        # min por acción: [-20, 40] → argmax = acción 1
        assert d.maximin(PAYOFFS) == 1

    def test_maximax(self) -> None:
        # max por acción: [100, 40] → argmax = acción 0
        assert d.maximax(PAYOFFS) == 0

    def test_hurwicz_optimistic(self) -> None:
        # α=1 → maximax → acción 0
        assert d.hurwicz(PAYOFFS, 1.0) == 0

    def test_hurwicz_pessimistic(self) -> None:
        # α=0 → maximin → acción 1
        assert d.hurwicz(PAYOFFS, 0.0) == 1

    def test_hurwicz_bad_alpha_raises(self) -> None:
        with pytest.raises(ValueError):
            d.hurwicz(PAYOFFS, 1.5)

    def test_savage(self) -> None:
        # regret: estado0 max=100 → [0,60]; estado1 max=40 → [60,0]
        # max regret por acción: [60,60] → empate, argmin = acción 0
        assert d.savage_minimax_regret(PAYOFFS) == 0

    def test_laplace(self) -> None:
        # media por acción: [40,40] → empate, argmax = acción 0
        assert d.laplace(PAYOFFS) == 0


class TestDynamicProgram:
    def test_unique_shortest_path(self) -> None:
        stages = [["A"], ["B", "C"], ["D"]]
        costs: dict[tuple[object, object], float] = {
            ("A", "B"): 2, ("A", "C"): 4, ("B", "D"): 3, ("C", "D"): 2
        }

        def cost(a: object, b: object, _i: int) -> float:
            return costs[(a, b)]

        res = d.dynamic_program(stages, cost)
        assert res.value == pytest.approx(5.0)
        assert res.policy == ["A", "B", "D"]  # 2+3=5 < 4+2=6

    def test_maximize(self) -> None:
        stages = [["A"], ["B", "C"], ["D"]]
        values: dict[tuple[object, object], float] = {
            ("A", "B"): 2, ("A", "C"): 4, ("B", "D"): 3, ("C", "D"): 2
        }

        def val(a: object, b: object, _i: int) -> float:
            return values[(a, b)]

        res = d.dynamic_program(stages, val, minimize=False)
        assert res.value == pytest.approx(6.0)  # 4+2 via C
        assert res.policy == ["A", "C", "D"]

    def test_too_few_stages_raises(self) -> None:
        with pytest.raises(ValueError):
            d.dynamic_program([["A"]], lambda a, b, i: 0.0)

    def test_empty_stage_raises(self) -> None:
        with pytest.raises(ValueError):
            d.dynamic_program([["A"], []], lambda a, b, i: 0.0)
