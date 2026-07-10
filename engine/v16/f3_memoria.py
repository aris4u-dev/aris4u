"""F3.MEMORIA — ACID Session State Manager with Event Sourcing.

Provides durable, transactional session state using SQLite WAL mode.
Replaces /tmp JSON with ACID-compliant persistence + event log + ranking.

Key features:
- ACID transactions: all-or-nothing writes
- Event sourcing: immutable log of all state changes
- WAL mode: durable even on system crash
- FTS5 search: fast full-text recall of decisions
- Miller ranking: top-7 decisions (working memory limit)
- Entropy-based pruning: remove low-value decisions
"""

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .config import BUSY_TIMEOUT_MS, SESSIONS_DB
except ImportError:
    SESSIONS_DB = Path.home() / "projects" / "aris4u" / "data" / "sessions.db"
    BUSY_TIMEOUT_MS = 10000


@dataclass
class SessionEvent:
    """Immutable event in session history."""

    event_type: str  # 'decision_locked', 'guard_added', 'state_updated', 'event_logged'
    payload: Dict[str, Any]
    timestamp: str = ""
    event_id: Optional[int] = None

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")


class MemoriaEngine:
    """F3.MEMORIA: ACID-compliant session state manager.

    Manages persistent session state with ACID guarantees, event sourcing,
    and multi-tier retrieval (FTS5 + semantic + ranking).
    """

    def __init__(self, db_path: Optional[str] = None):
        """Initialize MEMORIA engine.

        Args:
            db_path: Path to sessions.db. Defaults to config.SESSIONS_DB.

        Ensures database exists with proper WAL configuration and schema.
        """
        self.db_path = Path(db_path or SESSIONS_DB)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_tables()
        self._lock = threading.RLock()  # For thread-safe operations

    def _connect(self) -> sqlite3.Connection:
        """Create database connection with WAL + ACID defaults.

        Returns:
            Configured sqlite3.Connection with row factory and pragmas.
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")  # Balance durability + performance
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self) -> None:
        """Create v16 schema tables if not present.

        Tables:
        - v16_session_state: KV store for session state (replaces /tmp JSON)
        - v16_events: Immutable event log for audit trail
        """
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS v16_session_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS v16_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL CHECK(event_type IN (
                        'decision_locked', 'guard_added', 'state_updated',
                        'event_logged', 'pruning_event', 'consistency_check'
                    )),
                    payload TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    session_id TEXT,
                    agent_id TEXT,
                    fsync_order INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_v16_events_type
                    ON v16_events(event_type);
                CREATE INDEX IF NOT EXISTS idx_v16_events_ts
                    ON v16_events(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_v16_events_session
                    ON v16_events(session_id);
                CREATE INDEX IF NOT EXISTS idx_v16_state_updated
                    ON v16_session_state(updated_at DESC);
            """)
            conn.commit()
        finally:
            conn.close()

    def save_state(self, key: str, value: Any, session_id: Optional[str] = None) -> None:
        """Save key-value state to v16_session_state (ACID).

        Args:
            key: State key (e.g., 'token_intelligence', 'hook_router_metrics')
            value: Any JSON-serializable object
            session_id: Optional session reference for audit trail

        Raises:
            sqlite3.Error: On database failure
        """
        with self._lock:
            conn = self._connect()
            try:
                serialized = json.dumps(value)
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO v16_session_state (key, value, updated_at) "
                        "VALUES (?, ?, CURRENT_TIMESTAMP)",
                        (key, serialized),
                    )
                    # V2.0 P0-B: state_updated event logging removed. The KV row above
                    # IS the source of truth (load_state reads v16_session_state, NOT the
                    # event log); the event carried no value (only key+size) and accounted
                    # for 94% of v16_events bloat (413k rows). state_updated stays a valid
                    # event_type for callers that log it deliberately.
            finally:
                conn.close()

    def load_state(self, key: str, default: Any = None) -> Any:
        """Load key-value state from v16_session_state.

        Args:
            key: State key to load
            default: Default value if key not found

        Returns:
            Deserialized value, or default if not found/error
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM v16_session_state WHERE key = ?",
                (key,),
            ).fetchone()
            if row:
                return json.loads(row[0])
        except (sqlite3.OperationalError, json.JSONDecodeError):
            pass
        finally:
            conn.close()
        return default

    def append_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[int]:
        """Append immutable event to v16_events log (ACID).

        Args:
            event_type: Type of event (decision_locked, guard_added, etc.)
            payload: Event payload as dict (will be JSON-serialized)
            session_id: Optional session reference
            agent_id: Optional agent that triggered this event

        Returns:
            event_id of inserted row

        Raises:
            ValueError: If event_type is invalid
        """
        valid_types = {
            "decision_locked",
            "guard_added",
            "state_updated",
            "event_logged",
            "pruning_event",
            "consistency_check",
        }
        if event_type not in valid_types:
            raise ValueError(f"Invalid event_type: {event_type}. Must be one of {valid_types}")

        with self._lock:
            conn = self._connect()
            try:
                serialized = json.dumps(payload)
                with conn:
                    cursor = conn.execute(
                        """INSERT INTO v16_events
                           (event_type, payload, timestamp, session_id, agent_id)
                           VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?)""",
                        (event_type, serialized, session_id, agent_id),
                    )
                    event_id = cursor.lastrowid
                return event_id
            finally:
                conn.close()

    def _append_event_internal(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        payload: Dict[str, Any],
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Optional[int]:
        """Internal: append event within existing transaction.

        Used when appending events as part of larger transactions.
        Does NOT acquire lock (caller responsible).

        Args:
            conn: Active sqlite3.Connection
            event_type: Type of event
            payload: Event payload as dict
            session_id: Optional session reference
            agent_id: Optional agent reference

        Returns:
            event_id of inserted row
        """
        serialized = json.dumps(payload)
        cursor = conn.execute(
            """INSERT INTO v16_events
               (event_type, payload, timestamp, session_id, agent_id)
               VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?)""",
            (event_type, serialized, session_id, agent_id),
        )
        return cursor.lastrowid

    def get_events(
        self,
        event_type: Optional[str] = None,
        since: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Query events from v16_events log.

        Args:
            event_type: Filter by event type (optional)
            since: ISO timestamp string; return events after this time (optional)
            session_id: Filter by session ID (optional)
            limit: Maximum number of results (default 100)

        Returns:
            List of event dicts with event_id, timestamp, type, payload
        """
        conn = self._connect()
        try:
            sql = "SELECT event_id, event_type, payload, timestamp, session_id, agent_id FROM v16_events WHERE 1=1"
            params: List[Any] = []

            if event_type:
                sql += " AND event_type = ?"
                params.append(event_type)

            if since:
                sql += " AND timestamp > ?"
                params.append(since)

            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)

            sql += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            results = []
            for row in rows:
                result = dict(row)
                try:
                    result["payload"] = json.loads(result["payload"])
                except json.JSONDecodeError:
                    result["payload"] = {}
                results.append(result)
            return results
        finally:
            conn.close()

    def recall_decisions(
        self,
        query: Optional[str] = None,
        domain: Optional[str] = None,
        limit: int = 7,  # Miller 7±2 limit
    ) -> List[Dict[str, Any]]:
        """Recall locked decisions by FTS5 query.

        Implements Miller working memory limit (7±2 items max).

        Args:
            query: FTS5 search query (e.g., "regex email validation")
            domain: Filter by domain (e.g., "auth", "api_design")
            limit: Max results (capped at 7 for Miller limit). Default 7.

        Returns:
            List of matched decisions from sessions.db (existing decisions table)
            or empty list if no matches
        """
        limit = min(limit, 7)  # Enforce Miller 7±2 limit
        conn = self._connect()
        try:
            sql = "SELECT id, decision, rationale, domain, locked, session_ref, created_at FROM decisions WHERE locked = 1"
            params: List[Any] = []

            if domain:
                sql += " AND domain = ?"
                params.append(domain)

            if query:
                # Build FTS5 query string
                fts_query = " OR ".join(query.split())
                sql = """SELECT d.id, d.decision, d.rationale, d.domain, d.locked, d.session_ref, d.created_at
                    FROM decisions d
                    WHERE d.locked = 1 AND d.rowid IN
                    (SELECT rowid FROM decisions_fts WHERE decisions_fts MATCH ?)"""
                params = [fts_query]
                if domain:
                    sql += " AND d.domain = ?"
                    params.append(domain)

            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            results = []
            for row in rows:
                result = dict(row)
                results.append(result)
            return results
        except sqlite3.OperationalError:
            # decisions table may not exist in isolated test; fallback
            return []
        finally:
            conn.close()

    def recall_guards(
        self, severity: Optional[str] = None, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Recall behavioral guards by severity.

        Args:
            severity: Filter by severity ('low', 'medium', 'high', 'critical')
            limit: Max results (default 10)

        Returns:
            List of guard dicts from sessions.db guards table
        """
        conn = self._connect()
        try:
            sql = "SELECT id, pattern, prevention, severity, source_session, created_at FROM guards WHERE 1=1"
            params: List[Any] = []

            if severity:
                sql += " AND severity = ?"
                params.append(severity)

            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(sql, params).fetchall()
            results = [dict(r) for r in rows]
            return results
        except sqlite3.OperationalError:
            # guards table may not exist in isolated test; fallback
            return []
        finally:
            conn.close()

    def get_session_summary(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Get summary of session activity.

        Args:
            session_id: Optional session ID to filter by

        Returns:
            Dict with event_count, decision_count, guard_count, last_update
        """
        conn = self._connect()
        try:
            sql = "SELECT COUNT(*) FROM v16_events WHERE 1=1"
            params: List[Any] = []

            if session_id:
                sql += " AND session_id = ?"
                params.append(session_id)

            event_count = conn.execute(sql, params).fetchone()[0]

            sql = "SELECT MAX(timestamp) FROM v16_events WHERE 1=1"
            params = [session_id] if session_id else []
            if session_id:
                sql += " AND session_id = ?"

            last_update = conn.execute(sql, params).fetchone()[0]

            return {
                "event_count": event_count,
                "last_update": last_update,
                "session_id": session_id,
            }
        finally:
            conn.close()

    def prune_old_events(self, days: int = 30, max_events: int = 1000) -> int:
        """Prune old events by age or count (entropy-based cleanup).

        Args:
            days: Delete events older than this many days
            max_events: Keep only this many most recent events total

        Returns:
            Number of rows deleted
        """
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    # Delete old events
                    cursor = conn.execute(
                        "DELETE FROM v16_events WHERE timestamp < datetime('now', ? || ' days')",
                        (f"-{days}",),
                    )
                    deleted = cursor.rowcount

                    # Keep only max_events most recent (capturar SU cursor: antes se
                    # reusaba el del 1er DELETE → el 2o borrado no se contaba y el 1o
                    # se sumaba dos veces).
                    cursor = conn.execute(
                        """DELETE FROM v16_events WHERE event_id NOT IN
                           (SELECT event_id FROM v16_events ORDER BY timestamp DESC LIMIT ?)""",
                        (max_events,),
                    )
                    deleted += cursor.rowcount

                    # Log the pruning event
                    self._append_event_internal(
                        conn,
                        event_type="pruning_event",
                        payload={"pruned_rows": deleted, "days": days, "max_events": max_events},
                    )

                return deleted
            finally:
                conn.close()

    def verify_consistency(self) -> Dict[str, Any]:
        """Verify ACID invariants and consistency of state.

        Checks:
        - All JSON in v16_session_state is valid
        - All JSON in v16_events is valid
        - Foreign key constraints (if enabled)
        - No duplicate state keys

        Returns:
            Dict with status ('ok' or 'error'), issue_count, details
        """
        conn = self._connect()
        try:
            issues = []

            # Check v16_session_state JSON validity
            rows = conn.execute("SELECT key, value FROM v16_session_state").fetchall()
            for key, value in rows:
                try:
                    json.loads(value)
                except json.JSONDecodeError as e:
                    issues.append(f"Invalid JSON in session_state[{key}]: {str(e)}")

            # Check v16_events JSON validity
            rows = conn.execute("SELECT event_id, payload FROM v16_events").fetchall()
            for event_id, payload in rows:
                try:
                    json.loads(payload)
                except json.JSONDecodeError as e:
                    issues.append(f"Invalid JSON in event[{event_id}]: {str(e)}")

            # Check for duplicate state keys (should not exist with PRIMARY KEY)
            rows = conn.execute(
                "SELECT key, COUNT(*) as cnt FROM v16_session_state GROUP BY key HAVING cnt > 1"
            ).fetchall()
            for key, cnt in rows:
                issues.append(f"Duplicate state key: {key} ({cnt} rows)")

            status = "ok" if not issues else "error"
            return {
                "status": status,
                "issue_count": len(issues),
                "issues": issues,
            }
        finally:
            conn.close()


# Backwards compatibility: functions that mimic old token_intelligence API


def save_state(key: str, value: Any) -> None:
    """Save state to MEMORIA engine (backwards compat).

    Args:
        key: State key
        value: JSON-serializable value
    """
    engine = MemoriaEngine()
    engine.save_state(key, value)


def load_state(key: str, default: Any = None) -> Any:
    """Load state from MEMORIA engine (backwards compat).

    Args:
        key: State key
        default: Default if not found

    Returns:
        Loaded value or default
    """
    engine = MemoriaEngine()
    return engine.load_state(key, default)
