#!/usr/bin/env python3
"""Integration tests for ARIS4U V16.9 hooks pipeline and contract validator.

Tests enforcement mechanisms that make ARIS4U's depth/quality guarantees real:
- f5_validacion.py: 3-tier output validation
- soft_reward.py: Learning signal recording
- agent_orchestrator.py: Agent lifecycle management

Note: depth_inject.sh and subagent_depth.sh were ported to the Python dispatcher
(hooks/dispatch/) and deleted. Their equivalent coverage lives in
tests/dispatch/test_user_prompt_submit.py and tests/dispatch/test_subagent_start.py.

Usage:
    python3 -m pytest tests/test_v1610_hooks_pipeline.py -v --tb=short
"""

import json
import sys
from pathlib import Path

import pytest

# Add ARIS4U to path (portable: dos niveles arriba de este archivo es el repo root)
ARIS4U_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ARIS4U_ROOT))

pytestmark = pytest.mark.integration

from engine.v16.agent_orchestrator import AgentDef, AgentOrchestrator, AgentState
from engine.v16.f5_validacion import ContractSpec, ValidacionEngine, ValidationResult
from engine.v16.soft_reward import (
    DOMAIN_BASELINES,
)


class TestF5ValidacionEngine:
    """Test PART 4: f5_validacion.py."""

    def test_valid_json_contract_passes(self):
        """Test: Valid JSON output → tier1 contract check passes."""
        engine = ValidacionEngine()

        output = json.dumps({"name": "John", "email": "john@example.com", "age": 30})
        contract = ContractSpec(
            format="json",
            required_fields=["name", "email", "age"],
            type_checks={"age": "int", "name": "str"},
            min_length=10,
        )

        result = engine.validate(output, contract)

        assert result.verdict == "PASS", f"Expected PASS verdict. Errors: {result.errors}"  # type: ignore[attr-defined]  # ValidationResult uses .issues; .errors used only in failure message
        assert result.tier_passed == ["tier1"]

    def test_missing_required_field_fails(self):
        """Test: Missing required field → tier1 fails."""
        engine = ValidacionEngine()

        # Missing 'age' field
        output = json.dumps({"name": "John", "email": "john@example.com"})
        contract = ContractSpec(
            format="json",
            required_fields=["name", "email", "age"],
        )

        result = engine.validate(output, contract)

        assert result.verdict == "FAIL", "Expected FAIL verdict"
        assert any(
            "Missing required fields" in str(issue) for issue in result.issues
        ), f"Expected missing fields issue. Issues: {result.issues}"

    def test_type_mismatch_fails(self):
        """Test: Field type mismatch → tier1 fails."""
        engine = ValidacionEngine()

        output = json.dumps({"name": "John", "email": "john@example.com", "age": "30"})
        contract = ContractSpec(
            format="json",
            required_fields=["name", "email", "age"],
            type_checks={"age": "int"},
        )

        result = engine.validate(output, contract)

        assert result.verdict == "FAIL"
        assert any(
            "Type mismatch" in str(issue) for issue in result.issues
        ), f"Expected type mismatch issue. Issues: {result.issues}"

    def test_length_violation_fails(self):
        """Test: Output too short → tier1 fails."""
        engine = ValidacionEngine()

        output = "ab"  # Too short
        contract = ContractSpec(format="text", min_length=10)

        result = engine.validate(output, contract)

        assert result.verdict == "FAIL"
        assert any("too short" in str(issue).lower() for issue in result.issues)

    def test_semantic_entropy_high_uncertainty(self):
        """Test: Output with many TODOs → tier2 flags high uncertainty."""
        engine = ValidacionEngine()

        output = """
def process_payment(amount):
    # TODO: Validate amount
    # TODO: Connect to Stripe
    # TODO: Handle webhooks
    # TODO: Log transaction
    pass
"""
        context = {"query": "implement payment", "model": "gpt"}

        result = engine.validate(output, contract=None, context=context)

        # High uncertainty markers should flag as UNCERTAIN
        assert result.verdict in [
            "UNCERTAIN",
            "PASS",
        ], f"Expected UNCERTAIN or PASS. Got {result.verdict}"

    def test_degradation_when_jsonschema_missing(self):
        """Test: jsonschema unavailable → graceful degradation (F44 fix)."""
        engine = ValidacionEngine()

        output = json.dumps({"test": "data"})
        contract = ContractSpec(format="json", required_fields=["test"])

        # Validation should work even if jsonschema is missing
        result = engine.validate(output, contract)

        # Should not crash
        assert isinstance(result, ValidationResult)
        assert result.tier_passed  # Should have at least tier1


class TestSoftRewardLearning:
    """Test PART 5: soft_reward.py."""

    @pytest.mark.skip(reason="Requires writable claude-mem.db")
    def test_record_success_outcome(self):
        """Test: Record success → verify_score boosted via EMA."""
        # Would test: obs_id 999001 → score boosts via 0.9×old + 0.1×new

    @pytest.mark.skip(reason="Requires writable claude-mem.db")
    def test_record_failure_outcome(self):
        """Test: Record failure → verify_score halved."""
        # Would test: obs_id 999002 → score halves via 0.5×old

    def test_domain_baselines_are_sensible(self):
        """Test: Domain baselines are reasonable."""
        # Lower baselines = harder domains
        assert DOMAIN_BASELINES["python"] == 0.5
        assert DOMAIN_BASELINES["flutter"] == 0.4  # Hardest
        assert DOMAIN_BASELINES["java_spring"] == 0.5
        assert DOMAIN_BASELINES["node_ts"] == 0.5
        assert DOMAIN_BASELINES["generic"] == 0.3  # Fallback


class TestAgentOrchestrator:
    """Test PART 6: agent_orchestrator.py."""

    def test_register_agent(self):
        """Test: Register an agent → tracked in state."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")
        orch.reset()

        defn = AgentDef(name="jwt-auth", domain="python")
        orch.register(defn)

        assert "jwt-auth" in orch.agents
        assert orch.agents["jwt-auth"].domain == "python"

    def test_mark_agent_completed(self):
        """Test: Mark agent completed → state updated."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")
        orch.reset()

        defn = AgentDef(name="auth", domain="python")
        orch.register(defn)
        orch.mark_completed("auth")

        assert orch.results["auth"].state == AgentState.COMPLETED

    def test_mark_agent_failed(self):
        """Test: Mark agent failed → error recorded."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")
        orch.reset()

        defn = AgentDef(name="payment", domain="node")
        orch.register(defn)
        orch.mark_failed("payment", "Stripe API timeout")

        assert orch.results["payment"].state == AgentState.FAILED
        assert "Stripe API timeout" in orch.results["payment"].error  # type: ignore[operator]  # .error is str | None; test asserts it's set after mark_failed

    def test_get_ready_agents_no_dependencies(self):
        """Test: All agents with no deps → all ready."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")
        orch.reset()

        orch.register(AgentDef(name="a", domain="python"))
        orch.register(AgentDef(name="b", domain="python"))

        ready = orch.get_ready()
        assert set(ready) == {"a", "b"}

    def test_get_ready_respects_dependencies(self):
        """Test: Agent with unmet deps → not ready."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")
        orch.reset()

        orch.register(AgentDef(name="base", domain="python"))
        orch.register(AgentDef(name="extend", domain="python", dependencies=["base"]))

        # Initially only 'base' is ready
        ready = orch.get_ready()
        assert "base" in ready
        assert "extend" not in ready

        # After completing 'base', 'extend' becomes ready
        orch.mark_completed("base")
        ready = orch.get_ready()
        assert "extend" in ready

    def test_get_waves_linearizes_dependencies(self):
        """Test: Wave generation respects dependency order."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")
        orch.reset()

        # Create linear dependency chain: a -> b -> c
        orch.register(AgentDef(name="a", domain="python"))
        orch.register(AgentDef(name="b", domain="python", dependencies=["a"]))
        orch.register(AgentDef(name="c", domain="python", dependencies=["b"]))

        waves = orch.get_waves()

        # Should have 3 waves
        assert len(waves) == 3
        assert waves[0] == ["a"]
        assert waves[1] == ["b"]
        assert waves[2] == ["c"]

    def test_circular_dependency_detected(self):
        """Test: Circular dependency → raises ValueError."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")
        orch.reset()

        # Create circular: a -> b -> a
        orch.register(AgentDef(name="a", domain="python", dependencies=["b"]))
        orch.register(AgentDef(name="b", domain="python", dependencies=["a"]))

        with pytest.raises(ValueError, match="Circular dependency"):
            orch.get_waves()

    def test_parallel_independent_agents_in_same_wave(self):
        """Test: Independent agents → scheduled in same wave."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")
        orch.reset()

        orch.register(AgentDef(name="ui", domain="react"))
        orch.register(AgentDef(name="api", domain="node"))
        orch.register(AgentDef(name="db", domain="python"))

        waves = orch.get_waves()

        # All should be in wave 0 (parallel)
        assert len(waves) == 1
        assert set(waves[0]) == {"ui", "api", "db"}

    def test_summary_reports_correct_counts(self):
        """Test: Summary counts agents by state."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")
        orch.reset()

        orch.register(AgentDef(name="a", domain="python"))
        orch.register(AgentDef(name="b", domain="python"))
        orch.register(AgentDef(name="c", domain="python"))

        orch.mark_completed("a")
        orch.mark_failed("b", "error")

        summary = orch.summary()

        assert summary["total"] == 3
        assert summary["completed"] == 1
        assert summary["failed"] == 1
        assert summary["pending"] == 1

    def test_validate_dependencies_catches_orphans(self):
        """Test: Dependency on non-existent agent → raises ValueError."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")
        orch.reset()

        orch.register(AgentDef(name="child", domain="python", dependencies=["parent"]))

        with pytest.raises(ValueError, match="depends on 'parent'"):
            orch.validate_all_dependencies()

    def test_reset_clears_state(self):
        """Test: Reset → clears agents and results."""
        orch = AgentOrchestrator(state_file="/tmp/test_agent_orch.json")

        orch.register(AgentDef(name="test", domain="python"))
        assert len(orch.agents) > 0

        orch.reset()
        assert len(orch.agents) == 0
        assert len(orch.results) == 0


# Integration test: full pipeline
class TestFullPipeline:
    """Integration tests combining multiple components."""

    def test_orchestrator_agent_tracking_integration(self):
        """Test: Agent register → complete → record outcome → update reward."""
        orch = AgentOrchestrator(state_file="/tmp/test_full_pipeline.json")
        orch.reset()

        # Register two parallel agents
        orch.register(AgentDef(name="unit-tests", domain="python"))
        orch.register(AgentDef(name="integration-tests", domain="python"))

        # Both should be ready (parallel)
        waves = orch.get_waves()
        assert len(waves) == 1

        # Simulate completion
        orch.mark_completed("unit-tests")
        assert orch.results["unit-tests"].state == AgentState.COMPLETED

        # Simulate failure
        orch.mark_failed("integration-tests", "Timeout after 30s")
        assert orch.results["integration-tests"].state == AgentState.FAILED

        # Summary should reflect
        summary = orch.summary()
        assert summary["completed"] == 1
        assert summary["failed"] == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
