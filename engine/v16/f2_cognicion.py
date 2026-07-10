"""F2.COGNICION — Decision cascade for ARIS Engine V16.

Implements 4-decision routing with PID control, Boltzmann softmax, and dual-process
strategy selection (PID-based, replaces static thresholds).

Components:
- PIDController: Effort-level servo loop with anti-windup
- CognicionEngine: 4-decision cascade (dimensioning, effort, hooks, strategy)
- CognicionResult: Immutable output dataclass

State persisted to sessions.db (ACID, no /tmp files).
"""

import json
from dataclasses import dataclass
from typing import Optional
import math
import sqlite3

try:
    import numpy as np
except ImportError:
    np = None

from .config import SESSIONS_DB, DEPTH_LEVELS


@dataclass
class PIDGains:
    """PID controller tuning parameters (Kp, Ki, Kd)."""

    Kp: float
    Ki: float
    Kd: float

    def __repr__(self) -> str:
        return f"PID(Kp={self.Kp:.4f}, Ki={self.Ki:.6f}, Kd={self.Kd:.4f})"


@dataclass
class CognicionResult:
    """Output of 4-decision cascade from F2.COGNICION."""

    depth_levels: list[int]
    effort_level: str
    hooks_active: list[str]
    strategy: str
    pid_output: float
    confidence: float
    rationale: dict

    def to_dict(self) -> dict:
        """Serialize for sessions.db storage."""
        return {
            "depth_levels": self.depth_levels,
            "effort_level": self.effort_level,
            "hooks_active": self.hooks_active,
            "strategy": self.strategy,
            "pid_output": self.pid_output,
            "confidence": self.confidence,
            "rationale": self.rationale,
        }


class PIDController:
    """PID servo loop for effort-level target with anti-windup.

    Maintains stable effort_level despite budget fluctuations.
    State stored in sessions.db (ACID).
    """

    def __init__(
        self,
        gains: PIDGains,
        anti_windup_limit: float = 2.0,
        session_key: str = "pid_controller_state",
    ) -> None:
        """Initialize PID controller.

        Args:
            gains: PIDGains tuple (Kp, Ki, Kd)
            anti_windup_limit: Clamp integral to prevent windup
            session_key: Key in sessions.db for state persistence
        """
        self.Kp = gains.Kp
        self.Ki = gains.Ki
        self.Kd = gains.Kd
        self.anti_windup_limit = anti_windup_limit
        self.session_key = session_key
        self.integral = 0.0
        self.prev_error = 0.0
        self._load_state()

    def _load_state(self) -> None:
        """Load integral/prev_error from sessions.db."""
        try:
            db = sqlite3.connect(str(SESSIONS_DB))
            db.execute("PRAGMA busy_timeout = 10000")
            sql = "SELECT value FROM v15_session_state WHERE key = ?"
            row = db.execute(sql, (self.session_key,)).fetchone()
            db.close()

            if row:
                state = json.loads(row[0])
                self.integral = state.get("integral", 0.0)
                self.prev_error = state.get("prev_error", 0.0)
        except Exception:
            pass

    def _save_state(self) -> None:
        """Save integral/prev_error to sessions.db."""
        try:
            db = sqlite3.connect(str(SESSIONS_DB))
            db.execute("PRAGMA busy_timeout = 10000")
            state = {"integral": self.integral, "prev_error": self.prev_error}
            sql = """
                INSERT OR REPLACE INTO v15_session_state (key, value) VALUES (?, ?)
            """
            db.execute(sql, (self.session_key, json.dumps(state)))
            db.commit()
            db.close()
        except Exception:
            pass

    def update(self, target: float, actual: float, dt: float = 1.0) -> float:
        """Compute next effort adjustment via PID.

        Args:
            target: Target value (0.0-1.0 normalized budget%)
            actual: Actual value (0.0-1.0 normalized budget%)
            dt: Time step (seconds, default 1.0)

        Returns:
            Adjustment in effort level (typically -1.0 to +1.0).
            Output clamped to [0.0, 1.0].
        """
        error = target - actual

        # Proportional term
        P = self.Kp * error

        # Integral term with anti-windup
        self.integral += error * dt
        if self.Ki != 0:
            max_integral = self.anti_windup_limit / self.Ki
            self.integral = max(-max_integral, min(max_integral, self.integral))
        I = self.Ki * self.integral  # noqa: E741  (término Integral del PID; notación estándar P/I/D)

        # Derivative term
        D = 0.0
        if dt > 0:
            D = self.Kd * (error - self.prev_error) / dt

        self.prev_error = error

        output = P + I + D
        output = max(0.0, min(1.0, output))

        self._save_state()
        return output


class CognicionEngine:
    """F2.COGNICION — 4-decision cascade for query routing."""

    # Default PID gains (tuned for effort-level stability)
    DEFAULT_PID_GAINS = PIDGains(Kp=0.6, Ki=0.3, Kd=0.15)

    # Hook utility baseline values (used in Boltzmann routing)
    HOOK_UTILITIES = {
        "depth_inject": 0.8,
        "depth_validator": 0.7,
        "contract_guard": 0.6,
        "session_end": 0.4,
        "subagent_depth": 0.9,
        "ultraplan_capture": 0.5,
        "ultrareview_capture": 0.5,
    }

    def __init__(
        self,
        pid_gains: Optional[PIDGains] = None,
        all_available_hooks: Optional[list[str]] = None,
    ) -> None:
        """Initialize Cognicion engine.

        Args:
            pid_gains: PID controller gains (defaults to tuned values)
            all_available_hooks: List of all possible hook names
        """
        self.pid_gains = pid_gains or self.DEFAULT_PID_GAINS
        self.pid = PIDController(self.pid_gains)
        self.all_hooks = all_available_hooks or list(self.HOOK_UTILITIES.keys())

    def decide(
        self,
        intent: str,
        confidence: float,
        complexity: float,
        budget_remaining: int,
        budget_max: int = 200000,
    ) -> CognicionResult:
        """Execute 4-decision cascade.

        Args:
            intent: Query type (simple/fix/decision/implementation)
            confidence: Confidence in F1 classification (0.0-1.0)
            complexity: Query complexity (0-100)
            budget_remaining: Tokens remaining in budget
            budget_max: Total budget for budget% calculation

        Returns:
            CognicionResult with all 4 decisions
        """
        budget_pct = budget_remaining / max(1, budget_max)

        # DECISION 1: DIMENSIONING
        depth_levels = self._decision_dimensioning(intent, complexity, confidence)

        # DECISION 2: EFFORT_ROUTING (via PID)
        effort_level, pid_output = self._decision_effort_routing(budget_pct)

        # DECISION 3: HOOK_SELECTION
        hooks_active = self._decision_hook_selection(intent, effort_level)

        # DECISION 4: STRATEGY
        strategy = self._decision_strategy(complexity, confidence, budget_pct)

        rationale = {
            "intent": intent,
            "confidence": confidence,
            "complexity": complexity,
            "budget_pct": round(budget_pct * 100, 1),
            "decisions": {
                "dimensioning": depth_levels,
                "effort": effort_level,
                "hooks": hooks_active,
                "strategy": strategy,
            },
        }

        return CognicionResult(
            depth_levels=depth_levels,
            effort_level=effort_level,
            hooks_active=hooks_active,
            strategy=strategy,
            pid_output=pid_output,
            confidence=confidence,
            rationale=rationale,
        )

    def _decision_dimensioning(
        self, intent: str, complexity: float, confidence: float
    ) -> list[int]:
        """DECISION 1: How many depth levels?

        Uses base levels from config + complexity heuristics.

        Args:
            intent: Query type (simple/fix/decision/implementation)
            complexity: Query complexity (0-100)
            confidence: F1 confidence (0.0-1.0)

        Returns:
            list[int] of depth levels (e.g., [1, 3, 5, 7])
        """
        # Start with base levels from config
        base_levels = list(DEPTH_LEVELS.get(intent, [1]))
        if not base_levels:
            return [1]

        if intent == "simple":
            return [1]

        # Apply complexity heuristics
        levels = list(base_levels)

        # High complexity: keep all levels
        if complexity > 70:
            return sorted(set(levels + [8, 9, 10]))

        # Medium complexity: keep base levels
        if 30 <= complexity <= 70:
            return sorted(levels)

        # Low complexity: trim to first few levels
        if complexity < 30:
            return sorted(levels[:2])

        return sorted(levels)

    def _decision_effort_routing(self, budget_pct: float) -> tuple[str, float]:
        """DECISION 2: How much effort? (PID servo loop)

        Target: keep budget_pct near 80% (conservative target).
        Servo loop adjusts effort level to maintain target.

        Args:
            budget_pct: Current budget usage (0.0-1.0)

        Returns:
            Tuple of (effort_level_str, pid_output_float)
        """
        target = 0.80  # Conservative: keep 20% buffer
        pid_output = self.pid.update(target=target, actual=budget_pct)

        # Map PID output to effort level
        if pid_output < 0.25:
            effort = "low"
        elif pid_output < 0.5:
            effort = "medium"
        elif pid_output < 0.75:
            effort = "high"
        else:
            effort = "xhigh"

        return effort, pid_output

    def _decision_hook_selection(self, intent: str, effort_level: str) -> list[str]:
        """DECISION 3: Which hooks to activate? (Boltzmann softmax)

        Routes via exp[β * utility(hook)] probability distribution.

        Args:
            intent: Query type (affects hook utility)
            effort_level: Current effort level (affects selectivity)

        Returns:
            list[str] of active hook names
        """
        # Temperature parameter (lower = more selective)
        if effort_level == "low":
            temperature = 0.5  # Selective
        elif effort_level == "medium":
            temperature = 1.0  # Balanced
        elif effort_level == "high":
            temperature = 1.5  # Exploratory
        else:  # xhigh
            temperature = 2.0  # Max exploration

        # Compute utilities per hook
        utilities = {}
        for hook in self.all_hooks:
            base_utility = self.HOOK_UTILITIES.get(hook, 0.5)

            # Boost utilities for intent/effort combinations
            if intent == "implementation" and "depth" in hook:
                base_utility += 0.2
            if effort_level == "xhigh" and "review" in hook:
                base_utility += 0.15

            utilities[hook] = base_utility

        # Softmax routing (Boltzmann distribution)
        selected = self._softmax_select(utilities, temperature, threshold=0.1)
        return sorted(selected)

    def _decision_strategy(
        self, complexity: float, confidence: float, budget_pct: float
    ) -> str:
        """DECISION 4: Strategy (parallel/sequential/research_first)?

        Uses dual-process routing based on complexity vs budget.

        Args:
            complexity: Query complexity (0-100)
            confidence: F1 confidence (0.0-1.0)
            budget_pct: Budget remaining (0.0-1.0)

        Returns:
            str: "parallel_agents", "sequential", or "research_first"
        """
        # Parallel: simple + confident
        if complexity < 30 and confidence > 0.85:
            return "parallel_agents"

        # Research first: complex OR low budget
        if complexity > 70 or budget_pct < 0.2:
            return "research_first"

        # Sequential: default
        return "sequential"

    @staticmethod
    def _softmax_select(
        utilities: dict[str, float], temperature: float = 1.0, threshold: float = 0.1
    ) -> list[str]:
        """Boltzmann softmax selection via exp[utility/temperature].

        Args:
            utilities: dict[hook_name] -> utility_score
            temperature: Exploration parameter (lower=selective)
            threshold: Probability threshold for selection

        Returns:
            list[str] of selected hook names (P > threshold)
        """
        if not utilities:
            return []

        # Compute softmax probabilities
        scores = list(utilities.values())
        if temperature <= 0:
            temperature = 1.0

        # Numerical stability: subtract max before exp
        scores_array = np.array(scores) if np else scores
        if np:
            max_score = np.max(scores_array)
            exp_scores = np.exp((scores_array - max_score) / temperature)
            probs = exp_scores / np.sum(exp_scores)
            probs_list = probs.tolist()
        else:
            # Fallback without numpy
            max_score = max(scores)
            exp_scores = [math.exp((s - max_score) / temperature) for s in scores]
            total = sum(exp_scores)
            probs_list = [e / total for e in exp_scores]

        # Select hooks above threshold
        selected = []
        for (hook, utility), prob in zip(utilities.items(), probs_list):
            if prob > threshold:
                selected.append(hook)

        return selected if selected else list(utilities.keys())[0:1]


def create_cognicion_engine(
    pid_gains: Optional[PIDGains] = None,
    available_hooks: Optional[list[str]] = None,
) -> CognicionEngine:
    """Factory function to create a Cognicion engine instance.

    Args:
        pid_gains: Optional custom PID gains
        available_hooks: Optional list of available hooks

    Returns:
        Initialized CognicionEngine
    """
    return CognicionEngine(pid_gains=pid_gains, all_available_hooks=available_hooks)
