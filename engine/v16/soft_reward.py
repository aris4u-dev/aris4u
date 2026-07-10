"""Soft-reward EMA scoring sobre observations_local (V18 Fase E: sessions.db, no claude-mem).

Per V16.6 W4.3. Updates verify_score on post_agent_verify outcomes.
EMA: success → 0.9×old + 0.1×1.0; failure → 0.5×old.
Decay: 0.95^(weeks_since_verify) applied at query time.

Schema additions (observations table):
  - verify_score REAL DEFAULT NULL: Current EMA score [0, 1]
  - verify_count INTEGER DEFAULT 0: Number of verifications
  - verify_domain TEXT DEFAULT NULL: Inferred domain (python, flutter, java_spring, node_ts, generic)
"""

import logging
import sqlite3
from pathlib import Path
from typing import Literal, Optional

logger = logging.getLogger(__name__)

# V18 Fase E: DB_PATH → sessions.db (deja de tocar la claude-mem.db muerta). Este módulo
# (soft_reward, lazo NO cerrado, ~9 filas históricas) opera vía db_path explícito en tests;
# en producción su UPDATE sobre 'observations' es no-op fail-open (sessions.db no la tiene),
# lo cual es aceptable — el valor está en no recrear claude-mem, no en este scoring frágil.
try:
    from .config import SESSIONS_DB as DB_PATH
except Exception:
    DB_PATH = Path.home() / "projects" / "aris4u" / "data" / "sessions.db"

# Domain baseline scores (conservative)
DOMAIN_BASELINES = {
    "python": 0.5,
    "flutter": 0.4,
    "java_spring": 0.5,
    "node_ts": 0.5,
    "generic": 0.3,
}

# EMA smoothing factors
EMA_SUCCESS_ALPHA = 0.1  # 0.9*old + 0.1*new when success
EMA_FAILURE_ALPHA = 0.5  # 0.5*old when failure


def update_verify_score(
    observation_id: int,
    outcome: Literal["success", "failure"],
    db_path: Path = DB_PATH,
    verify_domain: Optional[str] = None,
) -> None:
    """Update verify_score for an observation using EMA smoothing.

    Args:
        observation_id: Row ID in observations table.
        outcome: "success" → boost (0.9*old + 0.1*1.0);
                 "failure" → halve (0.5*old)
        db_path: Path a la DB (V18: sessions.db; default DB_PATH).
        verify_domain: Optional domain override (else infer from DB).

    Raises:
        sqlite3.Error: If DB operation fails.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Fetch current verify_score + verify_count + domain
        cursor.execute(
            "SELECT verify_score, verify_count, verify_domain FROM observations WHERE id = ?",
            (observation_id,),
        )
        row = cursor.fetchone()

        if not row:
            logger.warning(f"Observation {observation_id} not found, skipping update")
            return

        old_score, verify_count, inferred_domain = row
        old_score = old_score or DOMAIN_BASELINES.get(inferred_domain or "generic", 0.3)
        verify_count = verify_count or 0

        # Apply EMA smoothing
        if outcome == "success":
            new_score = 0.9 * old_score + 0.1 * 1.0
        elif outcome == "failure":
            new_score = 0.5 * old_score
        else:
            logger.error(f"Invalid outcome: {outcome}")
            return

        new_count = verify_count + 1

        # Update DB
        cursor.execute(
            """
            UPDATE observations
            SET verify_score = ?, verify_count = ?
            WHERE id = ?
            """,
            (new_score, new_count, observation_id),
        )

        conn.commit()
        logger.info(
            f"Updated obs {observation_id}: {outcome} "
            f"(score {old_score:.3f} → {new_score:.3f}, count {new_count})"
        )

    except sqlite3.Error as e:
        logger.error(f"DB error updating observation {observation_id}: {e}")
        raise
    finally:
        if conn:
            conn.close()


def domain_weights(
    db_path: Path = DB_PATH,
) -> dict[str, float]:
    """Compute current domain weight averages.

    Queries observations table for per-domain average verify_score.
    Used for per-domain reranking and domain-specific baseline tuning.

    Args:
        db_path: Path a la DB (V18: sessions.db; default DB_PATH).

    Returns:
        dict[domain_name, avg_verify_score]

    Raises:
        sqlite3.Error: If DB query fails.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT verify_domain, COUNT(*) as cnt, AVG(verify_score) as avg_score
            FROM observations
            WHERE verify_score IS NOT NULL
            GROUP BY verify_domain
            ORDER BY avg_score DESC
            """)

        rows = cursor.fetchall()
        weights = {domain: score for domain, _cnt, score in rows}

        return weights

    except sqlite3.Error as e:
        logger.error(f"DB error querying domain weights: {e}")
        raise
    finally:
        if conn:
            conn.close()


def backfill_domain_baseline(
    db_path: Path = DB_PATH,
    force: bool = False,
) -> int:
    """Backfill verify_score for observations with NULL scores.

    Uses domain baseline or generic default (0.3).

    Args:
        db_path: Path a la DB (V18: sessions.db; default DB_PATH).
        force: If True, re-backfill all rows.

    Returns:
        Number of rows updated.

    Raises:
        sqlite3.Error: If DB operation fails.
    """
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        if force:
            where_clause = "1=1"
        else:
            where_clause = "verify_score IS NULL"

        # Assign baseline per domain
        cursor.execute(f"""
            UPDATE observations
            SET verify_score = CASE verify_domain
              WHEN 'python' THEN 0.5
              WHEN 'flutter' THEN 0.4
              WHEN 'java_spring' THEN 0.5
              WHEN 'node_ts' THEN 0.5
              ELSE 0.3
            END
            WHERE {where_clause}
            """)

        rows_updated = cursor.rowcount
        conn.commit()

        logger.info(f"Backfilled {rows_updated} observations with domain baselines")
        return rows_updated

    except sqlite3.Error as e:
        logger.error(f"DB error backfilling domain baseline: {e}")
        raise
    finally:
        if conn:
            conn.close()
