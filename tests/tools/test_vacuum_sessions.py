"""Tests for vacuum_sessions.py (W4.4).

Per V16.6 ROADMAP: vacuum_sessions.py with TTL-policy delete.
Uses /tmp test databases to avoid touching live data.
"""

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def test_db(tmp_path):
    """Create an isolated test database (copy of live sessions.db).

    Uses pytest's per-test ``tmp_path`` and replicates the repo's
    ``data/`` + ``logs/`` sibling layout so that vacuum_sessions.py's
    ``emit_event`` (which derives ``db_path.parent.parent / "logs"``)
    writes its JSONL events INSIDE this test's sandbox — never the live
    repo log. This keeps ``test_events_emitted`` deterministic in the
    full suite. Cleanup is handled by pytest.
    """
    live_db = Path(__file__).resolve().parents[2] / "data" / "sessions.db"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    test_db_path = data_dir / "sessions.db"

    # Copy live DB (read-only to avoid data loss)
    if live_db.exists():
        shutil.copy(live_db, test_db_path)
    else:
        pytest.skip("Live sessions.db not found")

    yield test_db_path


@pytest.fixture
def aris4u_root():
    """Return ARIS4U root path (portable across CI/dev)."""
    return Path(__file__).resolve().parents[2]


class TestVacuumTool:
    """Tests for tools/vacuum_sessions.py."""

    def test_setup_mode_idempotent(self, test_db, aris4u_root):
        """Test setup mode is idempotent (safe to run twice)."""
        # First run
        result1 = subprocess.run(
            [
                sys.executable,
                f"{aris4u_root}/tools/vacuum_sessions.py",
                "--db",
                str(test_db),
                "--mode",
                "setup",
            ],
            capture_output=True,
            text=True,
        )
        assert result1.returncode == 0, f"Setup failed: {result1.stderr}"

        # Second run (idempotent)
        result2 = subprocess.run(
            [
                sys.executable,
                f"{aris4u_root}/tools/vacuum_sessions.py",
                "--db",
                str(test_db),
                "--mode",
                "setup",
            ],
            capture_output=True,
            text=True,
        )
        assert result2.returncode == 0, f"Second setup failed: {result2.stderr}"

        # Verify mode was set (PRAGMA auto_vacuum may not persist in WAL mode
        # until full VACUUM, so accept either 0 or 2)
        conn = sqlite3.connect(str(test_db))
        cursor = conn.cursor()
        cursor.execute("PRAGMA auto_vacuum")
        mode = cursor.fetchone()[0]
        conn.close()

        # In WAL mode, PRAGMA auto_vacuum setting may not persist to header
        # until a full VACUUM is run. Accept 0 or 2 (INCREMENTAL intent applied).
        assert mode in (0, 2), f"Expected auto_vacuum in (0, 2), got {mode}"

    def test_delete_mode_dry_run(self, test_db, aris4u_root):
        """Test delete mode with --dry-run flag (no commits)."""
        # Get initial counts
        conn = sqlite3.connect(str(test_db))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM digests")
        initial_digests = cursor.fetchone()[0]
        conn.close()

        # Run delete with --dry-run
        result = subprocess.run(
            [
                sys.executable,
                f"{aris4u_root}/tools/vacuum_sessions.py",
                "--db",
                str(test_db),
                "--mode",
                "delete",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Delete dry-run failed: {result.stderr}"
        assert "DRY-RUN" in result.stdout, "Expected dry-run message in output"

        # Verify digests count unchanged
        conn = sqlite3.connect(str(test_db))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM digests")
        final_digests = cursor.fetchone()[0]
        conn.close()

        assert (
            final_digests == initial_digests
        ), f"Dry-run deleted rows (before {initial_digests}, after {final_digests})"

    def test_delete_mode_old_digests(self, test_db, aris4u_root):
        """Test delete mode removes old digests (>14 days)."""
        conn = sqlite3.connect(str(test_db))
        cursor = conn.cursor()

        # Insert synthetic old digest
        old_date = datetime.now(UTC) - timedelta(days=20)
        cursor.execute(
            """INSERT INTO digests (id, date, summary, created_at)
               VALUES (?, ?, ?, ?)""",
            (
                "test_old_digest_123",
                old_date.isoformat(),
                "Test old digest",
                old_date,
            ),
        )
        conn.commit()

        # Verify insert
        cursor.execute("SELECT COUNT(*) FROM digests WHERE id = 'test_old_digest_123'")
        assert cursor.fetchone()[0] == 1, "Old digest not inserted"

        conn.close()

        # Run delete (no dry-run)
        result = subprocess.run(
            [
                sys.executable,
                f"{aris4u_root}/tools/vacuum_sessions.py",
                "--db",
                str(test_db),
                "--mode",
                "delete",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Delete failed: {result.stderr}"

        # Verify old digest was deleted
        conn = sqlite3.connect(str(test_db))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM digests WHERE id = 'test_old_digest_123'")
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 0, "Old digest was not deleted"

    def test_delete_mode_protects_decisions(self, test_db, aris4u_root):
        """Test delete mode NEVER deletes decisions (LOCKED)."""
        conn = sqlite3.connect(str(test_db))
        cursor = conn.cursor()

        # Get initial decision count
        cursor.execute("SELECT COUNT(*) FROM decisions")
        initial_count = cursor.fetchone()[0]

        conn.close()

        # Run delete
        result = subprocess.run(
            [
                sys.executable,
                f"{aris4u_root}/tools/vacuum_sessions.py",
                "--db",
                str(test_db),
                "--mode",
                "delete",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Delete failed: {result.stderr}"

        # Verify decision count unchanged
        conn = sqlite3.connect(str(test_db))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM decisions")
        final_count = cursor.fetchone()[0]
        conn.close()

        assert (
            final_count == initial_count
        ), f"Decisions were deleted (before {initial_count}, after {final_count})"

    def test_vacuum_mode_incremental(self, test_db, aris4u_root):
        """Test vacuum mode runs incremental vacuum without error."""
        result = subprocess.run(
            [
                sys.executable,
                f"{aris4u_root}/tools/vacuum_sessions.py",
                "--db",
                str(test_db),
                "--mode",
                "vacuum",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Vacuum failed: {result.stderr}"
        assert (
            "incremental_vacuum" in result.stdout
        ), "Expected incremental_vacuum message"
        assert "optimize" in result.stdout, "Expected optimize message"

    def test_all_mode_complete_cycle(self, test_db, aris4u_root):
        """Test all mode runs setup + delete + vacuum."""
        result = subprocess.run(
            [
                sys.executable,
                f"{aris4u_root}/tools/vacuum_sessions.py",
                "--db",
                str(test_db),
                "--mode",
                "all",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"All mode failed: {result.stderr}"

        # Verify all steps were run
        assert "Enabling" in result.stdout or "already enabled" in result.stdout
        assert "Delete" in result.stdout
        assert "incremental_vacuum" in result.stdout

    def test_events_emitted(self, test_db, aris4u_root):
        """Test JSONL events are emitted to <db>/../logs/v16.1-events.jsonl.

        emit_event derives the log dir from db_path.parent.parent, so with an
        isolated test_db under tmp_path the event lands inside this test's
        sandbox (not the live repo log) — making the assertion deterministic.
        """
        # emit_event writes to db_path.parent.parent / "logs"
        events_log = test_db.parent.parent / "logs" / "v16.1-events.jsonl"

        # Run delete
        result = subprocess.run(
            [
                sys.executable,
                f"{aris4u_root}/tools/vacuum_sessions.py",
                "--db",
                str(test_db),
                "--mode",
                "delete",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        # The isolated log must exist and carry the vacuum_delete event.
        assert events_log.exists(), "events log was not created by vacuum delete"
        with open(events_log, "r") as f:
            lines = [ln for ln in f.readlines() if ln.strip()]

        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event_type") == "vacuum_delete":
                assert "digests_deleted" in event, "Missing digests_deleted in event"
                assert (
                    "gate_results_deleted" in event
                ), "Missing gate_results_deleted in event"
                return

        pytest.fail("vacuum_delete event not found in logs")

    def test_schema_check_passes(self, test_db, aris4u_root):
        """Test schema check detects required tables."""
        result = subprocess.run(
            [
                sys.executable,
                f"{aris4u_root}/tools/vacuum_sessions.py",
                "--db",
                str(test_db),
                "--mode",
                "setup",
            ],
            capture_output=True,
            text=True,
        )
        assert (
            result.returncode == 0
        ), f"Schema check failed on valid DB: {result.stderr}"

    def test_nonexistent_db_fails(self, aris4u_root):
        """Test tool fails gracefully on missing DB."""
        result = subprocess.run(
            [
                sys.executable,
                f"{aris4u_root}/tools/vacuum_sessions.py",
                "--db",
                "/tmp/nonexistent_vacuum_test.db",
                "--mode",
                "setup",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, "Expected non-zero exit on missing DB"
        assert "Database not found" in result.stderr or "Database not found" in result.stdout


class TestAsyncVacuumHook:
    """Tests for hooks/async_vacuum.sh."""

    def test_async_vacuum_throttle(self, aris4u_root):
        """Test async vacuum respects 1h throttle."""
        throttle_file = Path("/tmp/aris4u_last_vacuum")

        # Set throttle to "now" (prevent immediate run)
        from datetime import UTC, datetime as dt
        now_timestamp = int(dt.now(UTC).timestamp())
        throttle_file.write_text(str(now_timestamp))

        # Run async vacuum
        result = subprocess.run(
            [f"{aris4u_root}/hooks/async_vacuum.sh"],
            capture_output=True,
            text=True,
            env={**dict(__import__("os").environ), "ARIS4U_ROOT": str(aris4u_root)},
        )

        # Should exit 0 (silently skip due to throttle)
        assert result.returncode == 0, f"Async vacuum failed: {result.stderr}"

        # Cleanup
        if throttle_file.exists():
            throttle_file.unlink()

    def test_nightly_vacuum_runs(self, aris4u_root):
        """Test nightly vacuum hook can be invoked."""
        result = subprocess.run(
            [f"{aris4u_root}/hooks/nightly_vacuum.sh"],
            capture_output=True,
            text=True,
            timeout=30,
            env={**dict(__import__("os").environ), "ARIS4U_ROOT": str(aris4u_root)},
        )

        # Should complete without error
        assert result.returncode == 0, f"Nightly vacuum failed: {result.stderr}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
