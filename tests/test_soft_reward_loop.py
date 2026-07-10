"""Tests for soft_reward_loop Q-loop orchestrator.

Evaluates drift detection: que parámetros de adaptación cambian
a medida que el loop recibe rewards variables.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from engine.v16.soft_reward_loop import (
    record_reward,
    compute_adaptation,
    get_history,
    mark_applied,
    _compute_ema,
)


@pytest.fixture
def temp_db():
    """Temporary DB for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"

        # Monkey-patch DB_PATH in soft_reward_loop
        import engine.v16.soft_reward_loop as srl

        old_path = srl.DB_PATH
        srl.DB_PATH = db_path

        # Ensure schema
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reward_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL,
                reward REAL NOT NULL,
                caller TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                applied_at TEXT DEFAULT NULL,
                decision_type TEXT DEFAULT 'generic',
                context TEXT DEFAULT NULL,
                session_epoch INTEGER DEFAULT 0,
                UNIQUE(decision_id, caller)
            )
            """
        )
        conn.commit()
        conn.close()

        yield db_path

        # Restore
        srl.DB_PATH = old_path


def test_record_reward(temp_db):
    """Test recording a single reward signal."""
    result = record_reward(
        decision_id="session_001",
        reward=0.85,
        caller="agent_output_verifier",
        decision_type="compile_check",
    )
    assert result is True

    history = get_history(decision_id="session_001")
    assert len(history) >= 1
    assert history[0]["reward"] == 0.85
    assert history[0]["caller"] == "agent_output_verifier"


def test_reward_out_of_range(temp_db):
    """Test that rewards outside [0, 1] are rejected."""
    result = record_reward(
        decision_id="session_002",
        reward=1.5,  # Out of range
        caller="test",
    )
    assert result is False


def test_compute_adaptation_empty(temp_db):
    """Test adaptation with no history returns neutral multipliers."""
    adaptation = compute_adaptation("nonexistent_decision")
    assert adaptation["depth_multiplier"] == 1.0
    assert adaptation["strategy_confidence"] == 0.5
    assert adaptation["exemplar_budget_scale"] == 1.0


def test_compute_adaptation_single_signal(temp_db):
    """Test adaptation with one high reward signal."""
    record_reward("decision_A", reward=0.9, caller="test")

    adaptation = compute_adaptation("decision_A")

    # High reward → depth_multiplier should be >1.0
    assert adaptation["depth_multiplier"] > 1.0
    # High reward → strategy_confidence should be close to 0.9
    assert adaptation["strategy_confidence"] > 0.7


def test_compute_adaptation_low_rewards(temp_db):
    """Test adaptation with low rewards."""
    for i in range(5):
        record_reward(
            "decision_B",
            reward=0.2,
            caller=f"test_{i}",
        )

    adaptation = compute_adaptation("decision_B")

    # Low reward → depth_multiplier should be <1.0
    assert adaptation["depth_multiplier"] < 1.0
    # Low reward → strategy_confidence should be low
    assert adaptation["strategy_confidence"] < 0.5


@pytest.mark.timeout(30)
def test_drift_over_cycles(temp_db):
    """Test that adaptation drifts significantly over multiple reward cycles.

    Simulates 30 queries with extreme rewards (0.95 vs 0.05) to verify
    that compute_adaptation responds to reward distribution changes.
    Measures drift in depth_multiplier and exemplar_budget_scale.
    """
    decision_id = "test_drift_session"

    # Cycle 1: 15 very high-reward signals
    for i in range(15):
        record_reward(decision_id, reward=0.95, caller=f"cycle1_{i}")

    adapt_cycle1 = compute_adaptation(decision_id, window_size=15)
    depth_c1 = adapt_cycle1["depth_multiplier"]
    exemplar_c1 = adapt_cycle1["exemplar_budget_scale"]

    # Cycle 2: 15 very low-reward signals
    for i in range(15):
        record_reward(
            decision_id,
            reward=0.05,
            caller=f"cycle2_{i}",
        )

    # Recompute with only the low-reward window
    adapt_cycle2 = compute_adaptation(decision_id, window_size=15)
    depth_c2 = adapt_cycle2["depth_multiplier"]
    exemplar_c2 = adapt_cycle2["exemplar_budget_scale"]

    # Assert drift between high and low cycles
    drift_depth = abs(depth_c1 - depth_c2)
    drift_exemplar = abs(exemplar_c1 - exemplar_c2)

    # Require >=10% drift in at least one parameter
    # (With EMA alpha=0.3 on extreme values 0.95 vs 0.05, expect ~0.5 drift)
    assert drift_depth >= 0.10 or drift_exemplar >= 0.10, (
        f"Insufficient drift: depth={drift_depth:.3f}, exemplar={drift_exemplar:.3f}"
    )


def test_mark_applied(temp_db):
    """Test marking reward signal as applied."""
    record_reward("decision_C", reward=0.7, caller="test_apply")

    result = mark_applied("decision_C", "test_apply")
    assert result is True

    history = get_history(decision_id="decision_C")
    assert history[0]["applied_at"] is not None


def test_get_history_limit(temp_db):
    """Test history with limit."""
    for i in range(50):
        record_reward("decision_D", reward=0.5, caller=f"test_{i}")

    history = get_history(limit=10, decision_id="decision_D")
    assert len(history) <= 10


def test_ema_computation():
    """Test EMA function."""
    values = [0.1, 0.5, 0.9, 0.8, 0.7]
    ema = _compute_ema(values, alpha=0.3)

    # EMA should be bounded [0, 1]
    assert 0.0 <= ema <= 1.0
    # Should be closer to later values
    assert ema > 0.5


def test_ema_empty():
    """Test EMA with empty list."""
    ema = _compute_ema([], alpha=0.3)
    assert ema == 0.5  # Neutral default
