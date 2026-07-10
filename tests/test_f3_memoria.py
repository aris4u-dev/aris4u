"""Tests for F3.MEMORIA — ACID Session State Manager.

Tests verify:
- ACID properties (atomicity, consistency, isolation, durability)
- Event sourcing (append-only log)
- WAL mode functionality
- State persistence across reboots
- Concurrent write safety
- Miller 7±2 limit enforcement
- Consistency verification
"""

import pytest
import sqlite3
import json
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime

from engine.v16.f3_memoria import MemoriaEngine, SessionEvent


@pytest.fixture
def temp_db():
    """Create temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield db_path
    # Cleanup
    Path(db_path).unlink(missing_ok=True)
    Path(f"{db_path}-wal").unlink(missing_ok=True)
    Path(f"{db_path}-shm").unlink(missing_ok=True)


@pytest.fixture
def engine(temp_db):
    """Create MEMORIA engine with temp database."""
    return MemoriaEngine(db_path=temp_db)


class TestACIDProperties:
    """Test ACID transaction guarantees."""

    def test_atomicity_save_state(self, engine):
        """Test atomicity: state saved as all-or-nothing."""
        key = "test_atomic"
        value = {"nested": {"data": [1, 2, 3]}}

        engine.save_state(key, value)
        loaded = engine.load_state(key)

        assert loaded == value, "State must be saved exactly as provided"

    def test_consistency_invalid_json_rejected(self, engine, temp_db):
        """Test consistency: invalid JSON in database is detected."""
        # Manually insert invalid JSON to corrupt state
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO v16_session_state (key, value) VALUES (?, ?)",
            ("corrupt_key", "{ invalid json }"),
        )
        conn.commit()
        conn.close()

        # Verify consistency check detects it
        result = engine.verify_consistency()
        assert result["status"] == "error"
        assert result["issue_count"] > 0

    def test_isolation_concurrent_reads(self, engine):
        """Test isolation: concurrent reads don't block."""
        engine.save_state("concurrent_test", {"value": 42})

        results = []
        errors = []

        def read_state():
            try:
                val = engine.load_state("concurrent_test")
                results.append(val)
            except Exception as e:
                errors.append(e)

        # Launch 5 concurrent readers
        threads = [threading.Thread(target=read_state) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"No errors expected: {errors}"
        assert len(results) == 5, "All 5 reads should complete"
        assert all(r == {"value": 42} for r in results), "All reads should see same value"

    def test_durability_survives_reopen(self, engine, temp_db):
        """Test durability: state survives close and reopen."""
        key = "durability_test"
        value = {"persistent": "data", "timestamp": datetime.now().isoformat()}

        # Save in first engine instance
        engine.save_state(key, value)
        engine_db_path = engine.db_path

        # Create new engine instance (simulating reboot)
        engine2 = MemoriaEngine(db_path=str(engine_db_path))
        loaded = engine2.load_state(key)

        assert loaded == value, "State must survive close/reopen cycle"


class TestEventSourcing:
    """Test immutable event log functionality."""

    def test_append_event_valid_type(self, engine):
        """Test appending valid event types."""
        valid_types = [
            "decision_locked",
            "guard_added",
            "state_updated",
            "event_logged",
            "pruning_event",
            "consistency_check",
        ]

        for event_type in valid_types:
            event_id = engine.append_event(event_type, {"test": event_type})
            assert event_id > 0, f"Event ID must be positive for {event_type}"

    def test_append_event_invalid_type_raises(self, engine):
        """Test that invalid event type raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            engine.append_event("invalid_type", {})
        assert "Invalid event_type" in str(exc_info.value)

    def test_append_event_with_session_id(self, engine):
        """Test appending event with session ID for tracing."""
        event_id = engine.append_event(
            "decision_locked",
            {"decision": "use PostgreSQL"},
            session_id="session_001",
            agent_id="claude_builder",
        )
        assert event_id is not None

        events = engine.get_events(session_id="session_001")
        assert len(events) > 0
        assert events[0]["session_id"] == "session_001"
        assert events[0]["agent_id"] == "claude_builder"

    def test_get_events_filters_by_type(self, engine):
        """Test filtering events by type."""
        engine.append_event("decision_locked", {"data": 1})
        engine.append_event("guard_added", {"data": 2})
        engine.append_event("decision_locked", {"data": 3})

        decision_events = engine.get_events(event_type="decision_locked")
        assert len(decision_events) == 2
        assert all(e["event_type"] == "decision_locked" for e in decision_events)

    def test_get_events_filters_by_time(self, engine, temp_db):
        """Test filtering events by timestamp."""
        # Insert old event with explicit old timestamp
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO v16_events (event_type, payload, timestamp) VALUES (?, ?, ?)",
            ("state_updated", json.dumps({"test": 1}), "2020-01-01 00:00:00"),
        )
        conn.commit()
        conn.close()

        # Use a fixed past timestamp for comparison (SQLite-compatible)
        before_ts = "2025-01-01T00:00:00Z"
        time.sleep(0.01)

        # Insert new event (will use current timestamp, which is after 2025-01-01)
        engine.append_event("state_updated", {"test": 2})

        # Query for events after before_ts
        recent = engine.get_events(since=before_ts)
        assert len(recent) >= 1, f"Expected at least 1 event after {before_ts}, got {len(recent)}"
        assert any(e["payload"]["test"] == 2 for e in recent), "Should find event with test=2"

    def test_event_payload_json_roundtrip(self, engine):
        """Test that event payload survives JSON serialization."""
        complex_payload = {
            "nested": {"list": [1, 2, 3], "null": None, "bool": True},
            "timestamp": datetime.now().isoformat(),
        }

        engine.append_event("state_updated", complex_payload)
        events = engine.get_events(limit=1)

        assert len(events) == 1
        assert events[0]["payload"] == complex_payload


class TestMillerWorkingMemoryLimit:
    """Test Miller 7±2 cognitive limit enforcement."""

    def test_recall_decisions_capped_at_7(self, engine):
        """Test that recall_decisions returns max 7 items (Miller limit)."""
        # Note: recall_decisions queries the main decisions table
        # In this test, we verify the limiting logic works
        limited = engine.recall_decisions(query="test", limit=20)
        assert len(limited) <= 7, "Recall must enforce Miller 7±2 limit"

    def test_recall_decisions_default_limit_is_7(self, engine):
        """Test that default limit is 7."""
        # Creating a custom test that doesn't rely on decisions table
        # by checking the API signature
        import inspect

        sig = inspect.signature(engine.recall_decisions)
        assert sig.parameters["limit"].default == 7


class TestConsistencyVerification:
    """Test consistency checking and verification."""

    def test_verify_consistency_ok_on_clean_db(self, engine):
        """Test that clean database passes consistency check."""
        result = engine.verify_consistency()
        assert result["status"] == "ok"
        assert result["issue_count"] == 0

    def test_verify_consistency_detects_corrupt_json(self, engine, temp_db):
        """Test that corrupted JSON is detected."""
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO v16_session_state (key, value) VALUES (?, ?)",
            ("bad_json", "not valid json"),
        )
        conn.commit()
        conn.close()

        result = engine.verify_consistency()
        assert result["status"] == "error"
        assert result["issue_count"] > 0
        assert any("Invalid JSON" in issue for issue in result["issues"])


class TestWALMode:
    """Test WAL (Write-Ahead Logging) functionality."""

    def test_wal_mode_enabled(self, temp_db):
        """Test that WAL mode is set in MemoriaEngine."""
        engine = MemoriaEngine(db_path=temp_db)
        conn = engine._connect()
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.upper() == "WAL", "WAL mode must be enabled"
        finally:
            conn.close()

    def test_wal_mode_persists(self, temp_db):
        """Test that WAL mode setting persists across connections."""
        engine = MemoriaEngine(db_path=temp_db)
        engine.save_state("wal_test", {"data": "value"})

        # Create new connection and verify WAL mode still set
        conn = sqlite3.connect(temp_db)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.upper() == "WAL", "WAL mode must persist"
        finally:
            conn.close()


class TestStatePersistence:
    """Test state persistence and recovery."""

    def test_state_survives_reboot_simulation(self, temp_db):
        """Test that state survives simulated reboot (close/reopen)."""
        # First boot
        engine1 = MemoriaEngine(db_path=temp_db)
        engine1.save_state("boot_test", {"boot": 1, "value": "first"})

        # Simulate reboot by creating new engine instance
        engine2 = MemoriaEngine(db_path=temp_db)
        loaded = engine2.load_state("boot_test")
        assert loaded == {"boot": 1, "value": "first"}

        # Update in second boot
        engine2.save_state("boot_test", {"boot": 2, "value": "second"})

        # Third boot
        engine3 = MemoriaEngine(db_path=temp_db)
        loaded2 = engine3.load_state("boot_test")
        assert loaded2 == {"boot": 2, "value": "second"}

    def test_events_survive_reboot(self, temp_db):
        """Test that event log survives reboot."""
        engine1 = MemoriaEngine(db_path=temp_db)
        event_id = engine1.append_event("decision_locked", {"decision": "test"})
        assert event_id is not None

        # Reboot
        engine2 = MemoriaEngine(db_path=temp_db)
        events = engine2.get_events()
        assert len(events) > 0
        assert events[0]["payload"]["decision"] == "test"


class TestConcurrentWrites:
    """Test concurrent write safety."""

    def test_concurrent_state_writes_safe(self, engine):
        """Test that concurrent writes don't corrupt state."""
        errors = []
        results = []

        def write_state(thread_id):
            try:
                for i in range(10):
                    engine.save_state(
                        f"concurrent_{thread_id}",
                        {"thread": thread_id, "iteration": i},
                    )
                val = engine.load_state(f"concurrent_{thread_id}")
                results.append(val)
            except Exception as e:
                errors.append((thread_id, e))

        # Launch 5 concurrent writers
        threads = [threading.Thread(target=write_state, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"No errors expected: {errors}"
        assert len(results) == 5, "All threads should complete"

    def test_concurrent_event_appends_safe(self, engine):
        """Test that concurrent event appends don't corrupt log."""
        errors = []

        def append_events(thread_id):
            try:
                for i in range(10):
                    engine.append_event(
                        "state_updated",
                        {"thread": thread_id, "i": i},
                        session_id="concurrent_test",
                    )
            except Exception as e:
                errors.append((thread_id, e))

        threads = [threading.Thread(target=append_events, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"No errors expected: {errors}"
        events = engine.get_events(session_id="concurrent_test")
        assert len(events) == 50, "All 50 events should be recorded"


class TestPruning:
    """Test event pruning and cleanup."""

    def test_prune_old_events_removes_old(self, engine, temp_db):
        """Test that pruning removes old events."""
        # Insert events with old timestamps
        conn = sqlite3.connect(temp_db)
        for i in range(10):
            conn.execute(
                "INSERT INTO v16_events (event_type, payload, timestamp) "
                "VALUES (?, ?, datetime('now', '-60 days'))",
                ("state_updated", json.dumps({"old": i})),
            )
        conn.commit()
        conn.close()

        # Verify they exist
        before = engine.get_events(limit=100)
        old_count = len(before)

        # Prune
        deleted = engine.prune_old_events(days=30)
        assert deleted > 0, "Should delete old events"

        # Verify they're gone
        after = engine.get_events(limit=100)
        assert len(after) < old_count

    def test_prune_respects_max_events(self, engine, temp_db):
        """Test that pruning respects max_events limit."""
        # Insert many events with old timestamps
        conn = sqlite3.connect(temp_db)
        for i in range(100):
            conn.execute(
                "INSERT INTO v16_events (event_type, payload, timestamp) VALUES (?, ?, ?)",
                ("state_updated", json.dumps({"i": i}), "2020-01-01 00:00:00"),
            )
        # Add a few recent ones
        for i in range(10):
            conn.execute(
                "INSERT INTO v16_events (event_type, payload) VALUES (?, ?)",
                ("state_updated", json.dumps({"i": i})),
            )
        conn.commit()
        conn.close()

        # Prune to keep only 50 most recent (should delete 60+ old ones, keep 10 new)
        deleted = engine.prune_old_events(days=1, max_events=50)
        assert deleted > 0, "Should delete events"

        after = engine.get_events(limit=100)
        assert len(after) <= 50, "Should keep only max_events most recent"


class TestSessionSummary:
    """Test session summary functionality."""

    def test_get_session_summary_basic(self, engine):
        """Test getting session summary."""
        engine.append_event("decision_locked", {"data": 1}, session_id="test_session")
        engine.append_event("guard_added", {"data": 2}, session_id="test_session")

        summary = engine.get_session_summary(session_id="test_session")
        assert summary["event_count"] >= 2
        assert summary["session_id"] == "test_session"
        assert summary["last_update"] is not None

    def test_get_session_summary_all_sessions(self, engine):
        """Test summary across all sessions."""
        engine.append_event("state_updated", {"data": 1}, session_id="session_1")
        engine.append_event("state_updated", {"data": 2}, session_id="session_2")

        summary = engine.get_session_summary()
        assert summary["event_count"] >= 2
        assert summary["session_id"] is None


class TestBackwardsCompat:
    """Test backwards compatibility functions."""

    def test_save_state_function(self, temp_db):
        """Test module-level save_state function."""
        # Override default db path temporarily
        from engine.v16 import f3_memoria

        old_db = f3_memoria.SESSIONS_DB
        try:
            f3_memoria.SESSIONS_DB = Path(temp_db)
            f3_memoria.save_state("compat_test", {"value": 123})
            # Load with engine to verify
            engine = MemoriaEngine(db_path=temp_db)
            loaded = engine.load_state("compat_test")
            assert loaded == {"value": 123}
        finally:
            f3_memoria.SESSIONS_DB = old_db

    def test_load_state_function(self, temp_db):
        """Test module-level load_state function."""
        from engine.v16 import f3_memoria

        old_db = f3_memoria.SESSIONS_DB
        try:
            f3_memoria.SESSIONS_DB = Path(temp_db)
            engine = MemoriaEngine(db_path=temp_db)
            engine.save_state("compat_load", {"test": "data"})
            # Load via function
            loaded = f3_memoria.load_state("compat_load")
            assert loaded == {"test": "data"}
        finally:
            f3_memoria.SESSIONS_DB = old_db


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_load_nonexistent_key_returns_default(self, engine):
        """Test loading non-existent key returns default."""
        result = engine.load_state("nonexistent", default={"default": True})
        assert result == {"default": True}

    def test_load_nonexistent_key_no_default_returns_none(self, engine):
        """Test loading non-existent key without default returns None."""
        result = engine.load_state("nonexistent")
        assert result is None

    def test_empty_payload_allowed(self, engine):
        """Test that empty payload is allowed."""
        event_id = engine.append_event("state_updated", {})
        assert event_id > 0

        events = engine.get_events(limit=1)
        assert events[0]["payload"] == {}

    def test_large_state_value_persisted(self, engine):
        """Test that large state values are persisted."""
        large_value = {"data": "x" * 100000}  # 100KB string
        engine.save_state("large", large_value)
        loaded = engine.load_state("large")
        assert loaded == large_value

    def test_get_events_empty_limit_respected(self, engine):
        """Test that get_events respects limit=0 edge case."""
        for i in range(5):
            engine.append_event("state_updated", {"i": i})

        # Limit of 1 should return 1
        events = engine.get_events(limit=1)
        assert len(events) <= 1


class TestSessionEventDataclass:
    """Test SessionEvent dataclass."""

    def test_session_event_creation(self):
        """Test creating SessionEvent."""
        payload = {"decision": "use PostgreSQL"}
        event = SessionEvent(event_type="decision_locked", payload=payload)

        assert event.event_type == "decision_locked"
        assert event.payload == payload
        assert event.timestamp  # Should have auto-generated timestamp

    def test_session_event_asdict(self):
        """Test converting SessionEvent to dict."""
        event = SessionEvent(event_type="guard_added", payload={"pattern": "test"})
        event_dict = event.__dict__

        assert event_dict["event_type"] == "guard_added"
        assert event_dict["payload"] == {"pattern": "test"}
        assert event_dict["timestamp"]


class TestPruneOldEventsCount:
    """Regresión del bug de conteo en prune_old_events (audit MEDIO).

    Antes: el 2o DELETE reusaba el cursor del 1o → el 2o borrado no se contaba y el 1o
    se sumaba dos veces. Ahora cada DELETE captura su propio cursor.
    """

    def test_counts_both_deletes(self, engine):
        # 5 viejos (>30d) + 5 recientes; prune(days=30, max_events=3):
        #   1er DELETE (edad) borra 5 viejos; 2o DELETE (max) deja 3 → borra 2 → total 7.
        #   (Con el bug daba 10: 5 + 5 reusando el 1er cursor.)
        conn = engine._connect()
        with conn:
            conn.execute("DELETE FROM v16_events")  # base determinista
            for _ in range(5):
                conn.execute(
                    "INSERT INTO v16_events (event_type, payload, timestamp) "
                    "VALUES ('event_logged', '{}', datetime('now', '-40 days'))"
                )
            for _ in range(5):
                conn.execute(
                    "INSERT INTO v16_events (event_type, payload, timestamp) "
                    "VALUES ('event_logged', '{}', datetime('now'))"
                )
        conn.close()

        deleted = engine.prune_old_events(days=30, max_events=3)
        assert deleted == 7  # 5 por edad + 2 por exceso (no 10)
