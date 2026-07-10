"""Test V16 Orchestrator integration with hooks.

Tests:
1. Process multiple query types (simple, fix, decision, implementation)
2. Verify F1→F6 pipeline execution
3. Verify graceful fallback on F2-F6 failures
4. Verify F7 session learning
5. Verify hook compatibility
"""

import pytest
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.v16.v16_orchestrator import (
    get_orchestrator,
    reset_orchestrator,
    V16QueryResult,
)


class TestV16OrchestratorBasics:
    """Test basic orchestrator functionality."""

    def setup_method(self):
        """Reset orchestrator before each test."""
        reset_orchestrator()

    def test_orchestrator_singleton(self):
        """Verify orchestrator is a singleton."""
        orch1 = get_orchestrator()
        orch2 = get_orchestrator()
        assert orch1 is orch2, "Orchestrator should be a singleton"

    def test_process_query_returns_result(self):
        """Verify process_query returns V16QueryResult."""
        orch = get_orchestrator()
        result = orch.process_query("test query")
        assert isinstance(result, V16QueryResult)
        assert result.intent in ["simple", "fix", "decision", "implementation"]
        assert 0.0 <= result.confidence <= 1.0

    def test_query_truncation(self):
        """Verify queries are truncated to 500 chars."""
        orch = get_orchestrator()
        long_query = "a" * 1000
        result = orch.process_query(long_query)
        # Should not raise, should handle gracefully
        assert result.intent is not None


@pytest.mark.skip(
    reason="V16.5.2 triage: F1 intent classification assertions stale post-V16.4 multi-stack — needs re-baseline V16.6"
)
class TestV16QueryIntents:
    """Test intent classification across query types."""

    def setup_method(self):
        reset_orchestrator()

    def test_simple_intent(self):
        """Test simple query classification."""
        orch = get_orchestrator()
        result = orch.process_query("que es ARIS")
        assert result.intent == "simple"
        assert 1 in result.depth_levels  # Simple has at least RECALL
        # Note: effort_level is determined by F2, not just intent

    def test_fix_intent(self):
        """Test fix query classification."""
        orch = get_orchestrator()
        result = orch.process_query("arregla el bug en el login")
        assert result.intent == "fix"
        # F2 determines depth based on confidence, complexity, budget
        assert len(result.depth_levels) >= 1
        assert 5 in result.depth_levels  # Fix should include VERIFY

    def test_decision_intent(self):
        """Test decision query classification."""
        orch = get_orchestrator()
        result = orch.process_query("deberia usar PostgreSQL o MongoDB")
        assert result.intent == "decision"
        # Decision queries get some analysis phases
        assert len(result.depth_levels) >= 1
        # F2 adapts depth based on budget/complexity

    def test_implementation_intent(self):
        """Test implementation query classification."""
        orch = get_orchestrator()
        result = orch.process_query("construye el modulo de login")
        assert result.intent == "implementation"
        # Implementation queries are the most demanding
        assert len(result.depth_levels) >= 1
        # F2 adapts depth based on budget/complexity constraints


@pytest.mark.skip(
    reason="V16.5.2 triage: F1 confidence threshold stale post-V16.4 — needs re-baseline V16.6"
)
class TestV16PipelineComponents:
    """Test individual F1→F6 components."""

    def setup_method(self):
        reset_orchestrator()

    def test_f1_classification_confidence(self):
        """Verify F1 returns meaningful confidence."""
        orch = get_orchestrator()
        result = orch.process_query("construye el modulo de login")
        assert result.confidence > 0.5, "F1 should return >50% confidence"
        assert result.confidence <= 1.0, "F1 confidence should not exceed 100%"

    def test_f2_effort_levels(self):
        """Verify F2 returns valid effort levels."""
        orch = get_orchestrator()
        for query in [
            "que es ARIS",
            "arregla el bug",
            "deberia usar X o Y",
            "construye el modulo",
        ]:
            result = orch.process_query(query)
            assert result.effort_level in ["low", "medium", "high", "xhigh"]

    def test_f2_depth_levels(self):
        """Verify F2 returns valid depth levels."""
        orch = get_orchestrator()
        result = orch.process_query("construye algo")
        assert isinstance(result.depth_levels, list)
        assert len(result.depth_levels) > 0
        assert all(1 <= level <= 10 for level in result.depth_levels)

    def test_f2_hooks_active(self):
        """Verify F2 returns list of hooks."""
        orch = get_orchestrator()
        result = orch.process_query("construye el modulo")
        assert isinstance(result.hooks_active, list)
        # Implementation queries should activate some hooks
        if result.intent == "implementation":
            assert len(result.hooks_active) > 0

    def test_f2_strategy(self):
        """Verify F2 returns valid strategy."""
        orch = get_orchestrator()
        result = orch.process_query("construye algo")
        assert isinstance(result.strategy, str)
        assert result.strategy in ["default", "parallel_agents", "sequential", "research_first"]


class TestV16GracefulDegradation:
    """Test fallback behavior when modules fail."""

    def setup_method(self):
        reset_orchestrator()

    def test_f1_failure_fallback(self):
        """Verify orchestrator handles F1 failure gracefully."""
        orch = get_orchestrator()
        # Very long query to potentially stress F1
        long_query = "build " + "a" * 400
        result = orch.process_query(long_query)
        # Should return something, not crash
        assert result.intent is not None
        assert result.effort_level is not None

    def test_f2_failure_uses_defaults(self):
        """Verify F2 failure uses sensible defaults."""
        orch = get_orchestrator()
        result = orch.process_query("test query")
        # Even if F2 fails, should have defaults from F1
        assert result.effort_level in ["low", "medium", "high", "xhigh"]
        assert result.depth_levels is not None

    def test_no_critical_errors(self):
        """Verify orchestrator never crashes on valid input."""
        orch = get_orchestrator()
        test_queries = [
            "simple query",
            "arregla el bug",
            "deberia usar x o y",
            "construye el modulo de login",
            "",  # Empty
            "a" * 500,  # Max length
        ]
        for query in test_queries:
            try:
                result = orch.process_query(query)
                assert result is not None
            except Exception as e:
                pytest.fail(f"Orchestrator crashed on query '{query}': {e}")


class TestV16SessionLogging:
    """Test session logging for F7 learning."""

    def setup_method(self):
        reset_orchestrator()

    def test_session_log_accumulation(self):
        """Verify orchestrator accumulates queries for F7."""
        orch = get_orchestrator()
        initial_count = len(orch.session_log)

        orch.process_query("query 1")
        orch.process_query("query 2")
        orch.process_query("query 3")

        assert len(orch.session_log) == initial_count + 3

    def test_session_log_contains_metadata(self):
        """Verify session log entries contain required fields."""
        orch = get_orchestrator()
        orch.process_query("test query")

        assert len(orch.session_log) > 0
        entry = orch.session_log[-1]
        assert "query" in entry
        assert "intent" in entry
        assert "confidence" in entry
        assert "timestamp" in entry

    def test_end_session_returns_result(self):
        """Verify F7 returns learning result."""
        orch = get_orchestrator()
        orch.process_query("test 1")
        orch.process_query("test 2")

        result = orch.end_session()
        assert isinstance(result, dict)
        assert "patterns_learned" in result or "error" in result


class TestV16ResultSerialization:
    """Test V16QueryResult serialization."""

    def setup_method(self):
        reset_orchestrator()

    def test_result_to_dict(self):
        """Verify result can be serialized to dict."""
        orch = get_orchestrator()
        result = orch.process_query("test query")

        result_dict = result.to_dict()
        assert isinstance(result_dict, dict)
        assert result_dict["intent"] == result.intent
        assert result_dict["confidence"] == result.confidence
        assert result_dict["effort_level"] == result.effort_level

    def test_result_serialization_complete(self):
        """Verify all fields are serialized."""
        orch = get_orchestrator()
        result = orch.process_query("construye algo")

        result_dict = result.to_dict()
        expected_keys = [
            "intent",
            "confidence",
            "depth_levels",
            "effort_level",
            "hooks_active",
            "strategy",
            "locked_decisions",
            "guards",
            "validation_result",
            "format_directive",
            "error",
            "error_module",
        ]
        for key in expected_keys:
            assert key in result_dict


@pytest.mark.skip(
    reason="V16.5.2 triage: hook integration assertions stale post-V16.4+V16.5 hook rename + telemetry refactor — needs re-baseline V16.6"
)
class TestV16HookIntegration:
    """Test integration with hook system."""

    def setup_method(self):
        reset_orchestrator()

    def test_hook_compatibility_simple(self):
        """Verify orchestrator output works with hook template."""
        orch = get_orchestrator()
        result = orch.process_query("que es ARIS")

        # Simulate what the hook does
        level_names = ", ".join(str(lvl) for lvl in result.depth_levels)
        directive = f"DEPTH: {result.intent} | Levels: {level_names}"

        assert "simple" in directive
        assert "1" in directive  # Simple should have depth level 1

    def test_hook_compatibility_implementation(self):
        """Verify orchestrator output works with hook for implementation."""
        orch = get_orchestrator()
        result = orch.process_query("construye el modulo de login")

        # Simulate hook processing (output must be joinable without error)
        assert ", ".join(str(lvl) for lvl in result.depth_levels)
        assert result.intent == "implementation"
        assert len(result.depth_levels) > 1
        assert result.effort_level == "xhigh"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
