"""Tests for tools/project_timeline.active_builds().

Covers:
  - Returns running builds for the correct client with log_tail populated.
  - Does NOT return builds for another client (isolation).
  - Does NOT return builds with status 'done' or 'failed'.
  - Table absent → [].
  - DB file absent → [].
  - Log file absent/unreadable → log_tail = [].

All tests use tmp_path + isolated SQLite — the live data/sessions.db is
never touched.  No external processes are called.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.project_timeline import active_builds  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path, rows: list[dict]) -> Path:
    """Create an isolated sessions.db with build_runs seeded from ``rows``.

    Each row dict must have: client_id, repo_path, log_path, status, started_at.
    Optional: ended_at (defaults to NULL).

    Args:
        tmp_path: pytest tmp_path fixture.
        rows: List of dicts describing build_run rows to insert.

    Returns:
        Path to the created SQLite file.
    """
    db = tmp_path / "sessions.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE build_runs (
            run_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            intake_id  INTEGER NOT NULL DEFAULT 0,
            client_id  TEXT    NOT NULL,
            repo_path  TEXT    NOT NULL,
            log_path   TEXT    NOT NULL,
            status     TEXT    NOT NULL DEFAULT 'running',
            started_at TEXT    NOT NULL,
            ended_at   TEXT
        )
        """
    )
    for r in rows:
        conn.execute(
            "INSERT INTO build_runs (client_id, repo_path, log_path, status, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                r["client_id"],
                r["repo_path"],
                r["log_path"],
                r["status"],
                r["started_at"],
                r.get("ended_at"),
            ),
        )
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_active_builds_returns_running_for_client(tmp_path: Path) -> None:
    """active_builds returns rows with status='running' for the given client."""
    log = tmp_path / "build.log"
    log.write_text("step 1\nstep 2\nstep 3\n", encoding="utf-8")

    db = _make_db(
        tmp_path,
        [
            {
                "client_id": "aris4u",
                "repo_path": str(tmp_path / "repo"),
                "log_path": str(log),
                "status": "running",
                "started_at": "2026-07-07T10:00:00",
            }
        ],
    )

    result = active_builds(db_path=db, client_id="aris4u")

    assert len(result) == 1
    r = result[0]
    assert r["status"] == "running"
    assert r["repo_path"] == str(tmp_path / "repo")
    assert isinstance(r["run_id"], int)
    assert r["started_at"] == "2026-07-07T10:00:00"
    assert r["log_tail"] == ["step 1", "step 2", "step 3"]


def test_active_builds_client_isolation(tmp_path: Path) -> None:
    """Builds for a different client_id NEVER appear in the result."""
    log = tmp_path / "build.log"
    log.write_text("building…\n", encoding="utf-8")

    db = _make_db(
        tmp_path,
        [
            {
                "client_id": "client-c",  # different client
                "repo_path": str(tmp_path / "client-c-repo"),
                "log_path": str(log),
                "status": "running",
                "started_at": "2026-07-07T10:00:00",
            }
        ],
    )

    result = active_builds(db_path=db, client_id="aris4u")
    assert result == [], "should not leak builds from another client"


def test_active_builds_excludes_done_and_failed(tmp_path: Path) -> None:
    """Builds with status 'done' or 'failed' are excluded."""
    log = tmp_path / "build.log"
    log.write_text("done\n", encoding="utf-8")

    db = _make_db(
        tmp_path,
        [
            {
                "client_id": "aris4u",
                "repo_path": str(tmp_path / "r1"),
                "log_path": str(log),
                "status": "done",
                "started_at": "2026-07-07T09:00:00",
                "ended_at": "2026-07-07T09:30:00",
            },
            {
                "client_id": "aris4u",
                "repo_path": str(tmp_path / "r2"),
                "log_path": str(log),
                "status": "failed",
                "started_at": "2026-07-07T08:00:00",
                "ended_at": "2026-07-07T08:10:00",
            },
        ],
    )

    result = active_builds(db_path=db, client_id="aris4u")
    assert result == [], "done/failed builds must not appear"


def test_active_builds_table_absent_returns_empty(tmp_path: Path) -> None:
    """If build_runs table does not exist, returns [] without raising."""
    db = tmp_path / "no_table.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    result = active_builds(db_path=db, client_id="aris4u")
    assert result == []


def test_active_builds_db_absent_returns_empty(tmp_path: Path) -> None:
    """If the DB file does not exist, returns [] without raising."""
    absent = tmp_path / "ghost.db"
    result = active_builds(db_path=absent, client_id="aris4u")
    assert result == []


def test_active_builds_log_absent_returns_empty_tail(tmp_path: Path) -> None:
    """If the log file does not exist, log_tail is [] without raising."""
    db = _make_db(
        tmp_path,
        [
            {
                "client_id": "aris4u",
                "repo_path": str(tmp_path / "repo"),
                "log_path": str(tmp_path / "nonexistent.log"),
                "status": "running",
                "started_at": "2026-07-07T10:00:00",
            }
        ],
    )

    result = active_builds(db_path=db, client_id="aris4u")
    assert len(result) == 1
    assert result[0]["log_tail"] == []


def test_active_builds_log_tail_capped_at_15_lines(tmp_path: Path) -> None:
    """log_tail returns at most the last 15 lines, not all lines."""
    log = tmp_path / "big.log"
    lines = [f"line {i}" for i in range(30)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    db = _make_db(
        tmp_path,
        [
            {
                "client_id": "aris4u",
                "repo_path": str(tmp_path / "repo"),
                "log_path": str(log),
                "status": "running",
                "started_at": "2026-07-07T10:00:00",
            }
        ],
    )

    result = active_builds(db_path=db, client_id="aris4u")
    tail = result[0]["log_tail"]
    assert len(tail) == 15
    # Must be the LAST 15 lines.
    assert tail == lines[-15:]


def test_active_builds_multiple_running_same_client(tmp_path: Path) -> None:
    """Multiple running builds for the same client are all returned."""
    log1 = tmp_path / "log1.log"
    log2 = tmp_path / "log2.log"
    log1.write_text("run1\n", encoding="utf-8")
    log2.write_text("run2\n", encoding="utf-8")

    db = _make_db(
        tmp_path,
        [
            {
                "client_id": "aris4u",
                "repo_path": str(tmp_path / "r1"),
                "log_path": str(log1),
                "status": "running",
                "started_at": "2026-07-07T10:00:00",
            },
            {
                "client_id": "aris4u",
                "repo_path": str(tmp_path / "r2"),
                "log_path": str(log2),
                "status": "running",
                "started_at": "2026-07-07T10:05:00",
            },
        ],
    )

    result = active_builds(db_path=db, client_id="aris4u")
    assert len(result) == 2
    repos = {r["repo_path"] for r in result}
    assert str(tmp_path / "r1") in repos
    assert str(tmp_path / "r2") in repos


def test_active_builds_mixed_statuses_only_running_returned(tmp_path: Path) -> None:
    """Only 'running' rows appear when the table has a mix of statuses."""
    log = tmp_path / "build.log"
    log.write_text("building\n", encoding="utf-8")

    db = _make_db(
        tmp_path,
        [
            {
                "client_id": "aris4u",
                "repo_path": str(tmp_path / "running-repo"),
                "log_path": str(log),
                "status": "running",
                "started_at": "2026-07-07T10:00:00",
            },
            {
                "client_id": "aris4u",
                "repo_path": str(tmp_path / "done-repo"),
                "log_path": str(log),
                "status": "done",
                "started_at": "2026-07-07T09:00:00",
                "ended_at": "2026-07-07T09:30:00",
            },
        ],
    )

    result = active_builds(db_path=db, client_id="aris4u")
    assert len(result) == 1
    assert result[0]["repo_path"] == str(tmp_path / "running-repo")
