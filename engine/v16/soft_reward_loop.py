"""Soft Reward Q-loop Orchestrator.

Conecta soft_reward signal -> f7_aprendizaje -> depth_inject params.
Persiste en claude-mem.db tabla reward_signals.

V16.10 H44 — Cierra loop de retroalimentacion:
  1. agent_output_verifier calcula quality_score
  2. record_reward() persiste en reward_signals
  3. compute_adaptation() lee histórico y retorna params ajustados
  4. f7_aprendizaje aplica los params vía apply_reward_signals()
  5. depth_inject usa params para ajustar strategy siguiente sesión

Scope: llamada desde agent_output_verifier (decision_id=session_id),
        lectura desde f7_aprendizaje después de SELECT_EXEMPLARS.
"""

from __future__ import annotations

import sqlite3
import logging
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Optional, Dict, List

logger = logging.getLogger(__name__)

# V18 Fase E: reward_signals vive en sessions.db (texto propio), no en claude-mem.db muerta.
try:
    from .config import SESSIONS_DB as DB_PATH
except Exception:  # fallback defensivo si el import relativo falla en algún contexto
    DB_PATH = Path.home() / "projects" / "aris4u" / "data" / "sessions.db"


def _ensure_schema() -> None:
    """Create reward_signals table si no existe."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
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
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reward_decision
                ON reward_signals(decision_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reward_caller
                ON reward_signals(caller)
            """
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        logger.warning(f"Could not ensure schema: {e}")


def record_reward(
    decision_id: str,
    reward: float,
    caller: str,
    decision_type: str = "generic",
    context: Optional[str] = None,
) -> bool:
    """Record a reward signal for a decision.

    Args:
        decision_id: Unique identifier (session_id, query_id, etc).
        reward: Score [0, 1] representing quality/outcome.
        caller: Tool/module that generated this reward (e.g., "agent_output_verifier").
        decision_type: Type of decision (e.g., "depth_strategy", "exemplar_selection").
        context: Optional JSON or text context for future analysis.

    Returns:
        True if recorded successfully, False on error.
    """
    if not (0 <= reward <= 1):
        logger.error(f"Reward {reward} out of range [0, 1]")
        return False

    _ensure_schema()

    try:
        conn = sqlite3.connect(str(DB_PATH))
        now_iso = datetime.now(tz=UTC).isoformat()

        conn.execute(
            """
            INSERT OR REPLACE INTO reward_signals
            (decision_id, reward, caller, timestamp, decision_type, context)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (decision_id, reward, caller, now_iso, decision_type, context),
        )
        conn.commit()
        conn.close()

        logger.info(
            f"Recorded reward decision_id={decision_id} "
            f"reward={reward:.3f} caller={caller}"
        )
        return True

    except sqlite3.Error as e:
        logger.error(f"Failed to record reward: {e}")
        return False


def compute_adaptation(decision_id: str, window_size: int = 20) -> Dict[str, float]:
    """Compute adapted parameters based on reward history.

    Reads last `window_size` reward signals for this decision_id,
    computes EMA of rewards and returns multipliers for depth_inject params.

    Args:
        decision_id: Decision to analyze.
        window_size: Number of recent signals to consider.

    Returns:
        Dict with keys:
          - depth_multiplier: [0.5, 2.0] — adjust depth aggressiveness
          - strategy_confidence: [0.3, 1.0] — trust in current strategy
          - exemplar_budget_scale: [0.5, 2.0] — scale number of exemplars selected
          - pid_adaptive_tau: [0.5, 2.0] — time constant for PID tuning
    """
    _ensure_schema()

    try:
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT reward FROM reward_signals
            WHERE decision_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (decision_id, window_size),
        )
        rows = cursor.fetchall()
        # A0.7: marcar como aplicados para que no se re-procesen indefinidamente
        # (applied_at=NULL era el bug — los mismos signals se re-leían en cada sesión).
        if rows:
            now_iso = datetime.now(tz=UTC).isoformat()
            conn.execute(
                "UPDATE reward_signals SET applied_at = ? WHERE decision_id = ? AND applied_at IS NULL",
                (now_iso, decision_id),
            )
            conn.commit()
        conn.close()

        if not rows:
            # No history — return neutral multipliers
            return {
                "depth_multiplier": 1.0,
                "strategy_confidence": 0.5,
                "exemplar_budget_scale": 1.0,
                "pid_adaptive_tau": 1.0,
            }

        rewards = [r[0] for r in rows]
        # EMA: recent signals weighted higher
        ema = _compute_ema(rewards, alpha=0.3)

        # Map EMA [0, 1] to multipliers
        # Low EMA → reduce depth (avoid over-exploration of failing strategy)
        # High EMA → increase confidence (use strategy more aggressively)
        depth_mult = 0.5 + 1.5 * ema  # [0.5, 2.0]
        confidence = max(0.3, ema)  # [0.3, 1.0]
        exemplar_scale = 0.5 + 1.5 * ema  # [0.5, 2.0]
        tau = 0.5 + 1.5 * (1.0 - ema)  # [0.5, 2.0] — inverse: low reward → faster tuning

        return {
            "depth_multiplier": float(depth_mult),
            "strategy_confidence": float(confidence),
            "exemplar_budget_scale": float(exemplar_scale),
            "pid_adaptive_tau": float(tau),
        }

    except sqlite3.Error as e:
        logger.error(f"Failed to compute adaptation: {e}")
        return {
            "depth_multiplier": 1.0,
            "strategy_confidence": 0.5,
            "exemplar_budget_scale": 1.0,
            "pid_adaptive_tau": 1.0,
        }


def get_history(limit: int = 100, decision_id: Optional[str] = None) -> List[Dict]:
    """Fetch reward signal history.

    Args:
        limit: Max records to return.
        decision_id: Filter by decision_id (None = all).

    Returns:
        List of {id, decision_id, reward, caller, timestamp, applied_at, ...}
    """
    _ensure_schema()

    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if decision_id:
            cursor.execute(
                """
                SELECT id, decision_id, reward, caller, timestamp, applied_at,
                       decision_type, context
                FROM reward_signals
                WHERE decision_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (decision_id, limit),
            )
        else:
            cursor.execute(
                """
                SELECT id, decision_id, reward, caller, timestamp, applied_at,
                       decision_type, context
                FROM reward_signals
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )

        rows = cursor.fetchall()
        conn.close()

        return [dict(r) for r in rows]

    except sqlite3.Error as e:
        logger.error(f"Failed to fetch history: {e}")
        return []


def mark_applied(decision_id: str, caller: str) -> bool:
    """Mark a reward signal as applied (used for adaptation).

    Args:
        decision_id: Decision identifier.
        caller: Caller that originated the signal.

    Returns:
        True if marked, False on error.
    """
    _ensure_schema()

    try:
        conn = sqlite3.connect(str(DB_PATH))
        now_iso = datetime.now(tz=UTC).isoformat()

        conn.execute(
            """
            UPDATE reward_signals
            SET applied_at = ?
            WHERE decision_id = ? AND caller = ?
            """,
            (now_iso, decision_id, caller),
        )
        conn.commit()
        conn.close()
        return True

    except sqlite3.Error as e:
        logger.error(f"Failed to mark applied: {e}")
        return False


def _compute_ema(values: List[float], alpha: float = 0.3) -> float:
    """Compute exponential moving average of values.

    Args:
        values: List of rewards, oldest first.
        alpha: Smoothing factor [0, 1].

    Returns:
        EMA score [0, 1].
    """
    if not values:
        return 0.5

    ema = values[0]
    for v in values[1:]:
        ema = (1 - alpha) * ema + alpha * v

    return max(0.0, min(1.0, ema))
