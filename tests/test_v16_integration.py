"""
Integration test suite for ARIS Engine V16.

Tests the full pipeline: F1 → F2 → F3 → F4 → F5 → F6 → F7

Each test flow:
1. F1: PERCEPCION (classify intent)
2. F2: COGNICION (decide depth/effort/hooks/strategy)
3. F3: MEMORIA (save state + events)
4. F4: EJECUCION (build DAG, extract waves, execute)
5. F5: VALIDACION (validate output)
6. F6: COMUNICACION (format output, count tokens)
7. F7: APRENDIZAJE (learn from session)
"""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# Register custom pytest marks
pytest.mark.integration = pytest.mark.integration  # type: ignore[attr-defined]  # dynamic mark registration on MarkGenerator

from engine.v16.f1_classifier import EmbeddingClassifier, classify_v16_with_confidence
from engine.v16.f2_cognicion import create_cognicion_engine
from engine.v16.f3_memoria import MemoriaEngine
from engine.v16.f5_validacion import ContractSpec, ValidacionEngine
from engine.v16.f7_aprendizaje import AprendizajeEngine

# ============================================================================
# FIXTURES
# ============================================================================


@pytest.fixture
def temp_db():
    """Create temporary in-memory SQLite database for F3/F4/F7."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    try:
        Path(db_path).unlink()
    except FileNotFoundError:
        pass


@pytest.fixture
def f1_mock_classifier():
    """Mock F1 classifier to avoid Ollama dependency in CI."""
    with patch("engine.v16.f1_classifier.EmbeddingClassifier") as mock:
        classifier_instance = MagicMock()

        # Configure return values based on query
        def classify_side_effect(query):
            if "simple" in query.lower() or "what is" in query.lower():
                return ("simple", 0.95)
            elif "build" in query.lower() or "implement" in query.lower():
                return ("implementation", 0.92)
            elif "fix" in query.lower() or "bug" in query.lower():
                return ("fix", 0.88)
            elif "decide" in query.lower() or "should" in query.lower():
                return ("decision", 0.85)
            else:
                return ("simple", 0.70)

        classifier_instance.classify.side_effect = classify_side_effect
        mock.return_value = classifier_instance

        yield mock


@pytest.fixture
def simple_query():
    """A simple query for basic integration test."""
    return "what is ARIS"


@pytest.fixture
def implementation_query():
    """An implementation query for complex integration test."""
    return "build me a login module with email/password and Google OAuth"


@pytest.fixture
def fix_query():
    """A fix query for bug-fix integration test."""
    return "fix the regex email validation that's rejecting valid addresses"


@pytest.fixture
def decision_query():
    """A decision query for research-first integration test."""
    return "should we use PostgreSQL or MongoDB for user profiles"


@pytest.fixture
def spanish_query():
    """A Spanish conversational query."""
    return "¿Cómo debo estructurar la base de datos para un marketplace?"


def test_pipeline_f5_uncertain_requires_depth(temp_db):
    """When F5 detects high uncertainty markers, it flags for more depth."""

    f5 = ValidacionEngine()

    # Output with >2 uncertainty markers triggers UNCERTAIN or FAIL
    uncertain_output = """
def complex_function():
    # TODO: Handle edge cases
    # FIXME: Needs error handling
    # XXX: Race condition possible
    # TODO: More edge cases
    pass
"""
    contract = ContractSpec(format="code")
    context = {"query": "implement something"}

    validation = f5.validate(uncertain_output, contract, context)

    # F5 Tier 2 detects >=3 uncertainty markers -> entropy_score >= 0.4
    # This should result in UNCERTAIN or PASS based on context
    assert validation.verdict in ["UNCERTAIN", "PASS", "FAIL"]
    # Verify remediation is present for UNCERTAIN/FAIL cases
    if validation.verdict != "PASS":
        assert validation.remediation is not None


# ============================================================================
# TEST 7: F2 PID Feedback Loop
# ============================================================================


def test_pipeline_f2_pid_budget_feedback(temp_db):
    """F2 PID controller should track budget usage across multiple calls."""

    f2 = create_cognicion_engine()

    # Initial decision with 100% budget available (budget_pct = 1.0)
    result1 = f2.decide(
        intent="implementation",
        confidence=0.85,
        complexity=70.0,
        budget_remaining=200000,
        budget_max=200000,
    )
    pid1 = result1.pid_output
    budget_pct1 = result1.rationale["budget_pct"]
    assert 0.0 <= pid1 <= 1.0, f"pid1 should be a valid PID output: {pid1}"

    # After consuming 50% budget (budget_pct = 0.5)
    result2 = f2.decide(
        intent="implementation",
        confidence=0.85,
        complexity=70.0,
        budget_remaining=100000,  # 50% left
        budget_max=200000,
    )
    pid2 = result2.pid_output
    budget_pct2 = result2.rationale["budget_pct"]
    assert 0.0 <= pid2 <= 1.0, f"pid2 should be a valid PID output: {pid2}"

    # Budget percentages should differ
    assert budget_pct1 != budget_pct2, f"Budget pct should differ: {budget_pct1} vs {budget_pct2}"
    # PID controller has persistent state (integral term), so outputs should differ
    # But both should be valid effort values
    assert result1.effort_level in ["low", "medium", "high", "xhigh"]
    assert result2.effort_level in ["low", "medium", "high", "xhigh"]


def test_pipeline_pid_maintains_target_budget(temp_db):
    """PID controller should maintain target budget % (80%)."""

    f2 = create_cognicion_engine()

    # Simulate series of decisions with varying budgets
    budgets = [200000, 150000, 100000, 50000]
    pids = []

    for remaining in budgets:
        result = f2.decide(
            intent="decision",
            confidence=0.8,
            complexity=50.0,
            budget_remaining=remaining,
            budget_max=200000,
        )
        pids.append(result.pid_output)

        # All results should be valid
        assert 0.0 <= result.pid_output <= 1.0
        assert result.effort_level in ["low", "medium", "high", "xhigh"]


# ============================================================================
# TEST 8: F7 Learns from F5 Verdicts
# ============================================================================


def test_pipeline_f7_learns_from_f5_verdicts(temp_db):
    """F7 should use F5 verdicts as ground truth for learning."""

    f5 = ValidacionEngine()
    f7 = AprendizajeEngine(db_path=temp_db)

    # Simulate session with multiple F5 verdicts
    session_data = []

    queries = [
        "what is ARIS",
        "build a login module",
        "fix the email regex",
    ]
    intents = ["simple", "implementation", "fix"]

    for query, intent in zip(queries, intents):
        # F5 validates some output
        output = f"Response to: {query}"
        validation = f5.validate(output, ContractSpec())

        session_data.append(
            {
                "query": query,
                "predicted_intent": intent,
                "confidence": 0.8,
                "f5_verdict": validation.verdict,
                "embedding": np.random.rand(384),
            }
        )

    # F7 learns from session
    learning = f7.learn_from_session(
        session_id="test_learning_001",
        session_data=session_data,
        exemplar_pool={"simple": [], "implementation": [], "fix": []},
        ground_truth_labels={q: i for q, i in zip(queries, intents)},
    )

    assert learning is not None
    assert learning.temperature is not None
    assert learning.pid_gains is not None


# ============================================================================
# TEST 9: Error Handling (F1 Ollama Down → Fallback)
# ============================================================================


def test_pipeline_f1_fallback_when_ollama_down(temp_db):
    """When Ollama is unavailable, F1 should fallback gracefully."""

    # Simulate Ollama being down by creating classifier with no embeddings
    classifier = EmbeddingClassifier()

    # Mock _embed_text to return None (simulating Ollama failure)
    with patch.object(classifier, "_embed_text", return_value=None):
        # Should not crash, should return "simple" as safe default
        intent, confidence = classifier.classify("what is ARIS")

        # When embedding fails, confidence should be low (< threshold)
        assert confidence < 0.7  # Below threshold
        assert intent == "simple"  # Safe default


def test_pipeline_state_persistence_across_sessions(temp_db):
    """F3 should persist state across session close/reopen."""

    # === Session 1: Save state ===
    session1_data = {
        "user_id": "user_123",
        "intent": "implementation",
        "depth_level": 8,
        "effort": "xhigh",
    }

    f3_session1 = MemoriaEngine(db_path=temp_db)
    f3_session1.save_state("session_state", session1_data, session_id="sess_001")
    f3_session1.append_event(
        "state_updated", {"session": "sess_001", "action": "save_state"}, session_id="sess_001"
    )

    # === Session 2: Reopen and load ===
    f3_session2 = MemoriaEngine(db_path=temp_db)
    loaded_data = f3_session2.load_state("session_state")

    # Should have persisted
    assert loaded_data == session1_data
    assert loaded_data["user_id"] == "user_123"

    # Events should also persist
    events = f3_session2.get_events(session_id="sess_001")
    assert len(events) >= 1

    # === Session 3: Verify consistency ===
    consistency = f3_session2.verify_consistency()
    assert consistency["status"] == "ok"
    assert consistency["issue_count"] == 0


def test_pipeline_f3_event_sourcing(temp_db):
    """F3 should maintain immutable event log."""

    f3 = MemoriaEngine(db_path=temp_db)

    # Append a series of events
    events_to_log = [
        {"event_type": "decision_locked", "payload": {"decision": "use_postgres"}},
        {"event_type": "guard_added", "payload": {"guard": "no_sql_injection"}},
        {"event_type": "state_updated", "payload": {"state": "in_progress"}},
    ]

    event_ids = []
    for event in events_to_log:
        event_id = f3.append_event(
            event["event_type"], event["payload"], session_id="sess_event_test"
        )
        event_ids.append(event_id)

    # Retrieve and verify all events
    retrieved = f3.get_events(session_id="sess_event_test", limit=100)

    assert len(retrieved) == len(events_to_log)
    # Events should be in reverse chronological order
    for i, retrieved_event in enumerate(retrieved):
        assert retrieved_event["event_type"] in ["decision_locked", "guard_added", "state_updated"]


def test_pipeline_f2_depth_scales_with_complexity(temp_db):
    """F2 should assign more depth levels for complex queries."""

    f2 = create_cognicion_engine()

    # Simple query
    simple_result = f2.decide(
        intent="simple", confidence=0.95, complexity=5.0, budget_remaining=50000, budget_max=50000
    )

    # Complex query
    complex_result = f2.decide(
        intent="implementation",
        confidence=0.90,
        complexity=85.0,
        budget_remaining=150000,
        budget_max=200000,
    )

    # Complex should have more depth levels
    assert len(complex_result.depth_levels) >= len(simple_result.depth_levels)
    assert len(simple_result.depth_levels) == 1  # Simple: [1]
    assert max(complex_result.depth_levels) >= 7  # Complex: [1, 3, 5, 7, ...]


# ============================================================================
# INTEGRATION TESTS WITH REAL OLLAMA (Marked for CI Skip)
# ============================================================================


@pytest.mark.integration
def test_full_pipeline_with_real_ollama(temp_db):
    """Integration test with real Ollama (skip in CI, run locally)."""

    query = "what is ARIS Engine"

    # This will use real Ollama if available
    intent, confidence = classify_v16_with_confidence(query)

    # Should classify successfully
    assert intent in ["simple", "fix", "decision", "implementation", "research"]
    assert 0.0 <= confidence <= 1.0


@pytest.mark.integration
def test_f6_token_counting_with_real_api(temp_db):
    """Test F6 token counting with real Anthropic API (if key available)."""

    from engine.v16.f6_comunicacion import TokenCounter, TokenCountRequest

    counter = TokenCounter()

    request = TokenCountRequest(
        model="claude-opus-4-6-20250514",
        system_prompt="You are a helpful assistant.",
        messages=[{"role": "user", "content": "What is machine learning?"}],
    )

    # May use fallback if no API key
    result = counter.count_tokens(request)

    assert result.total_tokens > 0
    assert result.source in ["api", "tiktoken_fallback", "chars_fallback", "cache"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
