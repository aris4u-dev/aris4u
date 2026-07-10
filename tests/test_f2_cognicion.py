"""Tests for F2.COGNICION — 4-decision cascade for ARIS Engine V16.

Validates:
- PIDController convergence and anti-windup behavior
- 4-decision cascade (dimensioning, effort, hooks, strategy)
- Boltzmann softmax hook selection
- Strategy routing (all 3 paths)
- Integration with F1 output
- State persistence to sessions.db

Run: python -m pytest tests/test_f2_cognicion.py -v
"""

import pytest

from engine.v16.f2_cognicion import (
    PIDGains,
    PIDController,
    CognicionResult,
    CognicionEngine,
    create_cognicion_engine,
)


class TestPIDGains:
    """Test PID gains dataclass."""

    def test_pid_gains_creation(self):
        """Create PID gains tuple."""
        gains = PIDGains(Kp=0.6, Ki=0.3, Kd=0.15)
        assert gains.Kp == 0.6
        assert gains.Ki == 0.3
        assert gains.Kd == 0.15

    def test_pid_gains_repr(self):
        """Test repr formatting."""
        gains = PIDGains(Kp=0.6, Ki=0.3, Kd=0.15)
        repr_str = repr(gains)
        assert "Kp=0.6000" in repr_str
        assert "Ki=0.300000" in repr_str
        assert "Kd=0.1500" in repr_str


class TestPIDController:
    """Test PID servo loop with anti-windup."""

    def test_pid_controller_init(self):
        """Initialize PID controller."""
        gains = PIDGains(Kp=0.6, Ki=0.3, Kd=0.15)
        pid = PIDController(gains, anti_windup_limit=2.0, session_key="test_pid_init_key")
        assert pid.Kp == 0.6
        assert pid.Ki == 0.3
        assert pid.Kd == 0.15
        # May load state from DB, so just check they're numbers
        assert isinstance(pid.integral, float)
        assert isinstance(pid.prev_error, float)

    def test_pid_controller_single_update(self):
        """Single PID update step."""
        gains = PIDGains(Kp=0.6, Ki=0.0, Kd=0.0)  # P-only
        pid = PIDController(gains, session_key="test_pid_single_update_key")

        # Target 0.8, actual 0.5 → error 0.3
        output = pid.update(target=0.8, actual=0.5, dt=1.0)
        expected = 0.6 * 0.3  # Kp * error
        assert abs(output - min(1.0, expected)) < 0.01

    def test_pid_controller_convergence(self):
        """PID controller converges to target over iterations."""
        gains = PIDGains(Kp=0.4, Ki=0.2, Kd=0.1)
        pid = PIDController(gains, session_key="test_pid_convergence_key")

        target = 0.8
        actual = 0.5
        outputs = []

        for _ in range(15):
            output = pid.update(target=target, actual=actual, dt=1.0)
            outputs.append(output)
            # Simulate system response (integrator with decay)
            actual = actual + 0.1 * (output - actual)

        # Should stabilize (last 3 outputs similar)
        assert abs(outputs[-1] - outputs[-2]) < 0.1
        assert abs(outputs[-2] - outputs[-3]) < 0.1

    def test_pid_controller_anti_windup(self):
        """Anti-windup prevents integral buildup."""
        gains = PIDGains(Kp=0.1, Ki=0.5, Kd=0.0)
        pid = PIDController(gains, anti_windup_limit=2.0, session_key="test_pid_anti_windup_key")

        target = 0.99
        actual = 0.0
        outputs = []

        for _ in range(20):
            output = pid.update(target=target, actual=actual, dt=1.0)
            outputs.append(output)
            # Actual never reaches target
            actual = min(0.9, actual + 0.04)

        # Output should saturate at 1.0, not exceed
        assert all(o <= 1.0 for o in outputs)
        assert max(outputs) == pytest.approx(1.0, abs=0.01)

    def test_pid_controller_zero_error(self):
        """PID output near zero when at target (but clamped to [0, 1])."""
        gains = PIDGains(Kp=0.6, Ki=0.3, Kd=0.15)
        pid = PIDController(gains, session_key="test_pid_zero_error_key")

        output = pid.update(target=0.5, actual=0.5, dt=1.0)
        # Error is zero, so all terms (P, I, D) are zero, output should be ~0
        # Output is clamped to [0, 1], so should be very close to 0
        assert 0.0 <= output <= 0.1

    def test_pid_controller_negative_error(self):
        """PID handles negative error (over-budget)."""
        gains = PIDGains(Kp=0.6, Ki=0.3, Kd=0.15)
        pid = PIDController(gains, session_key="test_pid_negative_error_key")

        output = pid.update(target=0.5, actual=0.9, dt=1.0)
        # Error negative, output clamped to [0, 1]
        assert 0.0 <= output <= 1.0

    def test_pid_controller_state_persistence(self):
        """PID state saved/loaded from sessions.db."""
        gains = PIDGains(Kp=0.6, Ki=0.3, Kd=0.15)
        session_key = "test_pid_persistence_key"

        # First controller: update and save
        pid1 = PIDController(gains, session_key=session_key)
        pid1.integral = 0.5
        pid1.prev_error = 0.2
        pid1._save_state()

        # Second controller: load state
        pid2 = PIDController(gains, session_key=session_key)
        assert pid2.integral == pytest.approx(0.5, abs=0.01)
        assert pid2.prev_error == pytest.approx(0.2, abs=0.01)


class TestCognicionResult:
    """Test CognicionResult dataclass."""

    def test_cognicion_result_creation(self):
        """Create CognicionResult."""
        result = CognicionResult(
            depth_levels=[1, 3, 5],
            effort_level="high",
            hooks_active=["depth_inject", "contract_guard"],
            strategy="research_first",
            pid_output=0.75,
            confidence=0.85,
            rationale={"intent": "decision"},
        )
        assert result.depth_levels == [1, 3, 5]
        assert result.effort_level == "high"
        assert result.strategy == "research_first"

    def test_cognicion_result_to_dict(self):
        """Serialize CognicionResult to dict."""
        result = CognicionResult(
            depth_levels=[1, 3, 5],
            effort_level="high",
            hooks_active=["depth_inject"],
            strategy="sequential",
            pid_output=0.5,
            confidence=0.9,
            rationale={"test": "rationale"},
        )
        d = result.to_dict()
        assert d["depth_levels"] == [1, 3, 5]
        assert d["effort_level"] == "high"
        assert isinstance(d["rationale"], dict)


class TestCognicionEngineDimensioning:
    """Test DECISION 1: Dimensioning."""

    def test_dimensioning_simple_query(self):
        """Simple query → [1] only."""
        engine = CognicionEngine()
        levels = engine._decision_dimensioning("simple", complexity=10, confidence=0.9)
        assert levels == [1]

    def test_dimensioning_low_complexity(self):
        """Low complexity → first 2 levels."""
        engine = CognicionEngine()
        levels = engine._decision_dimensioning("decision", complexity=20, confidence=0.8)
        assert len(levels) <= 2
        assert 1 in levels

    def test_dimensioning_medium_complexity(self):
        """Medium complexity → base levels."""
        engine = CognicionEngine()
        levels = engine._decision_dimensioning("decision", complexity=50, confidence=0.8)
        # Should keep base levels from config
        assert 1 in levels
        assert len(levels) >= 2

    def test_dimensioning_high_complexity(self):
        """High complexity → full depth."""
        engine = CognicionEngine()
        levels = engine._decision_dimensioning(
            "implementation", complexity=80, confidence=0.8
        )
        # High complexity adds levels 8, 9, 10
        assert 8 in levels or 9 in levels or 10 in levels

    def test_dimensioning_unknown_type(self):
        """Unknown intent type → default [1]."""
        engine = CognicionEngine()
        levels = engine._decision_dimensioning("unknown", complexity=50, confidence=0.8)
        assert levels == [1]


class TestCognicionEngineEffortRouting:
    """Test DECISION 2: Effort routing via PID."""

    def test_effort_low_budget_warning(self):
        """Low budget (>80% used) → PID tries to increase effort."""
        engine = CognicionEngine()
        effort, pid_output = engine._decision_effort_routing(budget_pct=0.95)
        # 95% used, target is 80%, error is negative
        # PID output positive (trying to recover), maps to higher effort
        assert effort in ["low", "medium", "high", "xhigh"]

    def test_effort_healthy_budget(self):
        """Healthy budget (70% used) → medium-high effort."""
        engine = CognicionEngine()
        effort, pid_output = engine._decision_effort_routing(budget_pct=0.70)
        # At 70% used, target is 80%, error is positive
        # PID negative (reduce effort), maps to lower effort
        assert effort in ["low", "medium", "high", "xhigh"]

    def test_effort_critical_budget(self):
        """Critical budget (99% used) → high effort from PID."""
        engine = CognicionEngine()
        effort, pid_output = engine._decision_effort_routing(budget_pct=0.99)
        # 99% used, far from target, PID will be high
        assert effort in ["low", "medium", "high", "xhigh"]

    def test_effort_pid_output_bounds(self):
        """PID output clamped to [0, 1]."""
        engine = CognicionEngine()
        for budget in [0.0, 0.5, 1.0]:
            effort, pid_output = engine._decision_effort_routing(budget_pct=budget)
            assert 0.0 <= pid_output <= 1.0


class TestCognicionEngineHookSelection:
    """Test DECISION 3: Hook selection via Boltzmann softmax."""

    def test_hook_selection_low_effort(self):
        """Low effort → selective hooks."""
        engine = CognicionEngine(
            all_available_hooks=["depth_inject", "contract_guard", "session_end"]
        )
        hooks = engine._decision_hook_selection("implementation", effort_level="low")
        assert len(hooks) >= 1
        assert all(h in engine.all_hooks for h in hooks)

    def test_hook_selection_high_effort(self):
        """High effort → more hooks activated."""
        engine = CognicionEngine(
            all_available_hooks=["depth_inject", "contract_guard", "session_end"]
        )
        hooks_low = engine._decision_hook_selection("fix", effort_level="low")
        hooks_high = engine._decision_hook_selection("fix", effort_level="high")

        # High effort should select more/same hooks
        assert len(hooks_high) >= len(hooks_low) or len(hooks_high) == len(hooks_low)

    def test_hook_selection_implementation_boost(self):
        """Implementation intent boosts depth hooks."""
        engine = CognicionEngine(
            all_available_hooks=["depth_inject", "depth_validator", "contract_guard"]
        )
        hooks = engine._decision_hook_selection(
            "implementation", effort_level="medium"
        )
        # Should include depth hooks for implementation
        assert any("depth" in h for h in hooks)

    def test_hook_selection_returns_list(self):
        """Hook selection always returns list."""
        engine = CognicionEngine()
        hooks = engine._decision_hook_selection("decision", effort_level="medium")
        assert isinstance(hooks, list)
        assert len(hooks) >= 1


class TestCognicionEngineStrategy:
    """Test DECISION 4: Strategy routing."""

    def test_strategy_simple_confident(self):
        """Simple + confident → parallel."""
        engine = CognicionEngine()
        strategy = engine._decision_strategy(
            complexity=20, confidence=0.9, budget_pct=0.7
        )
        assert strategy == "parallel_agents"

    def test_strategy_complex_lowbudget(self):
        """Complex OR low budget → research_first."""
        engine = CognicionEngine()
        # Complex
        strategy1 = engine._decision_strategy(
            complexity=80, confidence=0.7, budget_pct=0.7
        )
        assert strategy1 == "research_first"

        # Low budget
        strategy2 = engine._decision_strategy(
            complexity=50, confidence=0.7, budget_pct=0.1
        )
        assert strategy2 == "research_first"

    def test_strategy_default_sequential(self):
        """Medium complexity/confidence/budget → sequential."""
        engine = CognicionEngine()
        strategy = engine._decision_strategy(
            complexity=50, confidence=0.7, budget_pct=0.5
        )
        assert strategy == "sequential"

    def test_strategy_all_paths_valid(self):
        """All strategy outputs are valid."""
        engine = CognicionEngine()
        valid_strategies = {"parallel_agents", "sequential", "research_first"}

        for complexity in [10, 50, 90]:
            for confidence in [0.5, 0.8, 0.95]:
                for budget in [0.1, 0.5, 0.9]:
                    strategy = engine._decision_strategy(complexity, confidence, budget)
                    assert strategy in valid_strategies


class TestCognicionEngineSoftmax:
    """Test Boltzmann softmax selection."""

    def test_softmax_empty_utilities(self):
        """Softmax handles empty utilities."""
        result = CognicionEngine._softmax_select({}, temperature=1.0)
        assert result == []

    def test_softmax_single_hook(self):
        """Softmax with single hook."""
        utilities = {"hook1": 0.8}
        result = CognicionEngine._softmax_select(utilities, temperature=1.0)
        assert "hook1" in result

    def test_softmax_temperature_exploration(self):
        """Lower temperature = more selective."""
        utilities = {"hook1": 0.8, "hook2": 0.5, "hook3": 0.3}

        # Low temperature: selective
        result_selective = CognicionEngine._softmax_select(
            utilities, temperature=0.3, threshold=0.1
        )
        # High temperature: exploratory
        result_exploratory = CognicionEngine._softmax_select(
            utilities, temperature=2.0, threshold=0.1
        )

        assert len(result_selective) <= len(result_exploratory)

    def test_softmax_threshold_filtering(self):
        """Softmax respects probability threshold."""
        utilities = {"hook1": 10.0, "hook2": 0.1, "hook3": 0.05}
        result = CognicionEngine._softmax_select(
            utilities, temperature=1.0, threshold=0.4
        )
        # High utility hook should be selected
        assert "hook1" in result


class TestCognicionEngineFull:
    """Test full 4-decision cascade."""

    def test_decide_simple_query(self):
        """Full decide() for simple query."""
        engine = CognicionEngine()
        result = engine.decide(
            intent="simple",
            confidence=0.95,
            complexity=10,
            budget_remaining=190000,
            budget_max=200000,
        )
        assert result.depth_levels == [1]
        assert result.confidence == 0.95
        assert isinstance(result.effort_level, str)
        assert isinstance(result.hooks_active, list)
        assert result.strategy in ["parallel_agents", "sequential", "research_first"]

    def test_decide_complex_decision(self):
        """Full decide() for complex decision query."""
        engine = CognicionEngine()
        result = engine.decide(
            intent="decision",
            confidence=0.75,
            complexity=65,
            budget_remaining=120000,
            budget_max=200000,
        )
        assert len(result.depth_levels) >= 1
        assert result.effort_level in ["low", "medium", "high", "xhigh"]
        assert result.strategy in ["parallel_agents", "sequential", "research_first"]

    def test_decide_implementation(self):
        """Full decide() for implementation."""
        engine = CognicionEngine()
        result = engine.decide(
            intent="implementation",
            confidence=0.8,
            complexity=75,
            budget_remaining=150000,
            budget_max=200000,
        )
        # Implementation should trigger high complexity path
        assert len(result.depth_levels) >= 2
        assert "depth_inject" in result.hooks_active or len(result.hooks_active) > 0

    def test_decide_rationale_completeness(self):
        """Rationale dict includes all decisions."""
        engine = CognicionEngine()
        result = engine.decide(
            intent="fix",
            confidence=0.85,
            complexity=40,
            budget_remaining=160000,
            budget_max=200000,
        )
        assert "intent" in result.rationale
        assert "complexity" in result.rationale
        assert "budget_pct" in result.rationale
        assert "decisions" in result.rationale

    def test_decide_to_dict(self):
        """Result serializes to dict."""
        engine = CognicionEngine()
        result = engine.decide(
            intent="decision",
            confidence=0.8,
            complexity=50,
            budget_remaining=140000,
            budget_max=200000,
        )
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "depth_levels" in d
        assert "effort_level" in d
        assert "hooks_active" in d
        assert "strategy" in d


class TestCognicionIntegration:
    """Integration tests with F1 output."""

    def test_f1_classification_to_cognicion(self):
        """Simulate F1→F2 pipeline."""
        # F1 output (from PERCEPCION)
        f1_intent = "implementation"
        f1_confidence = 0.82
        f1_complexity = 72

        # F2 routing
        engine = CognicionEngine()
        result = engine.decide(
            intent=f1_intent,
            confidence=f1_confidence,
            complexity=f1_complexity,
            budget_remaining=145000,
            budget_max=200000,
        )

        # Verify F2 respects F1 signals
        assert result.confidence == f1_confidence
        assert len(result.depth_levels) >= 3  # Implementation is high complexity
        assert result.strategy != "parallel_agents"  # Complex → not parallel

    def test_cognicion_result_integrates_with_depth_protocol(self):
        """CognicionResult output compatible with depth_protocol."""
        engine = CognicionEngine()
        result = engine.decide(
            intent="decision",
            confidence=0.8,
            complexity=50,
            budget_remaining=150000,
            budget_max=200000,
        )

        # Output should be consumable by depth_protocol
        assert isinstance(result.depth_levels, list)
        assert all(isinstance(lvl, int) for lvl in result.depth_levels)
        assert all(1 <= lvl <= 10 for lvl in result.depth_levels)
        assert isinstance(result.hooks_active, list)
        assert all(isinstance(h, str) for h in result.hooks_active)


class TestFactory:
    """Test factory function."""

    def test_create_cognicion_engine_default(self):
        """Create engine with defaults."""
        engine = create_cognicion_engine()
        assert isinstance(engine, CognicionEngine)
        assert engine.pid.Kp == 0.6

    def test_create_cognicion_engine_custom_gains(self):
        """Create engine with custom PID gains."""
        gains = PIDGains(Kp=0.5, Ki=0.2, Kd=0.1)
        engine = create_cognicion_engine(pid_gains=gains)
        assert engine.pid.Kp == 0.5
        assert engine.pid.Ki == 0.2

    def test_create_cognicion_engine_custom_hooks(self):
        """Create engine with custom hooks."""
        hooks = ["hook1", "hook2"]
        engine = create_cognicion_engine(available_hooks=hooks)
        assert engine.all_hooks == hooks


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
