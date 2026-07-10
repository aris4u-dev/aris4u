"""Tests for compute_amplification_score and write_amplification_score (E3 Batch E/O).

Uses an in-memory SQLite DB with the minimal schema required so tests never
touch the real data/sessions.db.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from engine.v16.session_manager import (
    _read_session_signals_from_log,
    compute_amplification_score,
    write_amplification_score,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE recall_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recall_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    session_id TEXT DEFAULT ''
);
CREATE TABLE recall_feedback (
    recall_id TEXT PRIMARY KEY,
    useful INTEGER NOT NULL,
    marked_at TEXT NOT NULL,
    method TEXT DEFAULT 'manual',
    score REAL,
    detail TEXT
);
CREATE TABLE amplification_scores (
    session_id TEXT PRIMARY KEY,
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    recalls_useful INTEGER NOT NULL DEFAULT 0,
    recalls_total INTEGER NOT NULL DEFAULT 0,
    f1_useful INTEGER NOT NULL DEFAULT 0,
    f1_total INTEGER NOT NULL DEFAULT 0,
    capabilities_adopted INTEGER NOT NULL DEFAULT 0,
    guard_blocks INTEGER NOT NULL DEFAULT 0,
    total_turns INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0.0,
    signals_note TEXT
);
"""


@pytest.fixture()
def mem_db() -> sqlite3.Connection:
    """In-memory DB with the tables needed for amplification_score."""
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript(_SCHEMA)
    db.commit()
    return db


def _insert_recalls(db: sqlite3.Connection, session_id: str, pairs: list[tuple[str, int]]) -> None:
    """Insert (recall_id, useful) pairs scoped to session_id."""
    db.executemany(
        "INSERT INTO recall_events (recall_id, ts, session_id) VALUES (?, '2026-07-06', ?)",
        [(rid, session_id) for rid, _ in pairs],
    )
    db.executemany(
        "INSERT INTO recall_feedback (recall_id, useful, marked_at) VALUES (?, ?, '2026-07-06')",
        [(rid, u) for rid, u in pairs],
    )
    db.commit()


# ---------------------------------------------------------------------------
# compute_amplification_score
# ---------------------------------------------------------------------------


class TestComputeAmplificationScore:
    def test_no_recalls_score_zero(self, mem_db: sqlite3.Connection) -> None:
        result = compute_amplification_score("sess-empty", db=mem_db)
        assert result["score"] == 0.0
        assert result["recalls_total"] == 0
        assert result["recalls_useful"] == 0

    def test_all_useful_score_one(self, mem_db: sqlite3.Connection) -> None:
        _insert_recalls(mem_db, "sess-all", [("r1", 1), ("r2", 1), ("r3", 1)])
        result = compute_amplification_score("sess-all", db=mem_db)
        assert result["score"] == pytest.approx(1.0)
        assert result["recalls_useful"] == 3
        assert result["recalls_total"] == 3

    def test_partial_useful_score(self, mem_db: sqlite3.Connection) -> None:
        _insert_recalls(mem_db, "sess-half", [("r10", 1), ("r11", 0), ("r12", 1), ("r13", 0)])
        result = compute_amplification_score("sess-half", db=mem_db)
        assert result["score"] == pytest.approx(0.5)
        assert result["recalls_useful"] == 2
        assert result["recalls_total"] == 4

    def test_recalls_without_feedback_score_zero(self, mem_db: sqlite3.Connection) -> None:
        mem_db.execute(
            "INSERT INTO recall_events (recall_id, ts, session_id) VALUES ('r20', '2026', 'sess-nofb')"
        )
        mem_db.execute(
            "INSERT INTO recall_events (recall_id, ts, session_id) VALUES ('r21', '2026', 'sess-nofb')"
        )
        mem_db.commit()
        # No recall_feedback rows — LEFT JOIN yields NULL useful → COALESCE → 0
        result = compute_amplification_score("sess-nofb", db=mem_db)
        assert result["score"] == 0.0
        assert result["recalls_total"] == 2
        assert result["recalls_useful"] == 0

    def test_other_session_not_counted(self, mem_db: sqlite3.Connection) -> None:
        _insert_recalls(mem_db, "sess-A", [("rA1", 1)])
        _insert_recalls(mem_db, "sess-B", [("rB1", 0), ("rB2", 0)])
        result_a = compute_amplification_score("sess-A", db=mem_db)
        result_b = compute_amplification_score("sess-B", db=mem_db)
        assert result_a["recalls_total"] == 1
        assert result_a["recalls_useful"] == 1
        assert result_a["score"] == pytest.approx(1.0)
        assert result_b["recalls_total"] == 2
        assert result_b["recalls_useful"] == 0
        assert result_b["score"] == pytest.approx(0.0)

    def test_adoption_gap_signals_zero(self, mem_db: sqlite3.Connection) -> None:
        result = compute_amplification_score("sess-gaps", db=mem_db)
        assert result["f1_useful"] == 0
        assert result["f1_total"] == 0
        assert result["capabilities_adopted"] == 0
        assert result["guard_blocks"] == 0
        assert result["total_turns"] == 0

    def test_signals_note_non_empty(self, mem_db: sqlite3.Connection) -> None:
        result = compute_amplification_score("sess-note", db=mem_db)
        assert result["signals_note"] is not None
        assert len(result["signals_note"]) > 0
        # Must mention the three tracked gaps
        assert "f1" in result["signals_note"]
        assert "capability" in result["signals_note"]
        assert "guard" in result["signals_note"]

    def test_result_has_all_keys(self, mem_db: sqlite3.Connection) -> None:
        result = compute_amplification_score("sess-keys", db=mem_db)
        expected = {
            "session_id", "recalls_useful", "recalls_total",
            "f1_useful", "f1_total", "capabilities_adopted",
            "guard_blocks", "total_turns", "score", "signals_note",
        }
        assert set(result.keys()) == expected

    def test_score_rounded_to_4dp(self, mem_db: sqlite3.Connection) -> None:
        # 1 useful out of 3 = 0.3333...
        _insert_recalls(mem_db, "sess-round", [("rR1", 1), ("rR2", 0), ("rR3", 0)])
        result = compute_amplification_score("sess-round", db=mem_db)
        assert result["score"] == round(1 / 3, 4)

    def test_session_id_preserved_in_result(self, mem_db: sqlite3.Connection) -> None:
        result = compute_amplification_score("my-unique-session", db=mem_db)
        assert result["session_id"] == "my-unique-session"


# ---------------------------------------------------------------------------
# write_amplification_score
# ---------------------------------------------------------------------------


class TestWriteAmplificationScore:
    def test_row_written_to_db(self, mem_db: sqlite3.Connection, tmp_path) -> None:
        _insert_recalls(mem_db, "sess-write", [("rW1", 1), ("rW2", 0)])

        db_path = tmp_path / "sessions.db"
        file_db = sqlite3.connect(str(db_path))
        file_db.executescript(_SCHEMA)
        file_db.executemany(
            "INSERT INTO recall_events (recall_id, ts, session_id) VALUES (?, '2026', ?)",
            [("rW1", "sess-write"), ("rW2", "sess-write")],
        )
        file_db.executemany(
            "INSERT INTO recall_feedback (recall_id, useful, marked_at) VALUES (?, ?, '2026')",
            [("rW1", 1), ("rW2", 0)],
        )
        file_db.commit()
        file_db.close()

        with patch("engine.v16.session_manager.SESSIONS_DB", db_path):
            write_amplification_score("sess-write")

        verify = sqlite3.connect(str(db_path))
        row = verify.execute(
            "SELECT session_id, recalls_useful, recalls_total, score "
            "FROM amplification_scores WHERE session_id = ?",
            ("sess-write",),
        ).fetchone()
        verify.close()

        assert row is not None
        assert row[0] == "sess-write"
        assert row[1] == 1   # recalls_useful
        assert row[2] == 2   # recalls_total
        assert abs(row[3] - 0.5) < 1e-6

    def test_upsert_idempotent(self, tmp_path) -> None:
        """Second write with same session_id replaces, not duplicates."""
        db_path = tmp_path / "sessions.db"
        file_db = sqlite3.connect(str(db_path))
        file_db.executescript(_SCHEMA)
        file_db.commit()
        file_db.close()

        with patch("engine.v16.session_manager.SESSIONS_DB", db_path):
            write_amplification_score("sess-idem")
            write_amplification_score("sess-idem")

        verify = sqlite3.connect(str(db_path))
        count = verify.execute(
            "SELECT COUNT(*) FROM amplification_scores WHERE session_id = ?",
            ("sess-idem",),
        ).fetchone()[0]
        verify.close()
        assert count == 1


# ---------------------------------------------------------------------------
# _read_session_signals_from_log — Batch O live signals (f1 + guard_blocks)
# ---------------------------------------------------------------------------


def _write_events(path: Path, events: list[dict]) -> None:
    """Write a list of dicts as JSONL to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


class TestReadSessionSignalsFromLog:
    """Verify f1_feedback and guard_block events are counted per session_id.

    All tests use ARIS4U_EVENTS_LOG env var (the canonical override) to point
    _read_session_signals_from_log at a tmp file — avoids SESSIONS_DB path
    resolution and is robust to any real ARIS4U_EVENTS_LOG in the environment.
    """

    def test_f1_feedback_useful_counted(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        _write_events(log, [
            {"event": "f1_feedback", "session_id": "s1", "useful": True, "call_id": "c1"},
            {"event": "f1_feedback", "session_id": "s1", "useful": True, "call_id": "c2"},
            {"event": "f1_feedback", "session_id": "s1", "useful": False, "call_id": "c3"},
            {"event": "f1_feedback", "session_id": "OTHER", "useful": True, "call_id": "c4"},
        ])
        with patch.dict(os.environ, {"ARIS4U_EVENTS_LOG": str(log)}):
            counts = _read_session_signals_from_log("s1")
        assert counts["f1_total"] == 3
        assert counts["f1_useful"] == 2

    def test_f1_feedback_other_session_excluded(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        _write_events(log, [
            {"event": "f1_feedback", "session_id": "OTHER", "useful": True, "call_id": "c1"},
        ])
        with patch.dict(os.environ, {"ARIS4U_EVENTS_LOG": str(log)}):
            counts = _read_session_signals_from_log("s1")
        assert counts["f1_total"] == 0
        assert counts["f1_useful"] == 0

    def test_phi_guard_block_counted(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        _write_events(log, [
            {"event": "phi_to_external_blocked", "session_id": "s2", "tool": "Bash"},
            {"event": "phi_to_external_blocked", "session_id": "s2", "tool": "WebFetch"},
            {"event": "phi_to_external_blocked", "session_id": "OTHER", "tool": "Bash"},
        ])
        with patch.dict(os.environ, {"ARIS4U_EVENTS_LOG": str(log)}):
            counts = _read_session_signals_from_log("s2")
        assert counts["guard_blocks"] == 2

    def test_migration_lint_blocked_counted(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        _write_events(log, [
            {"event": "migration_lint_blocked", "session_id": "s3", "stack": "supabase"},
            {"event": "migration_lint_blocked", "session_id": "OTHER", "stack": "flyway"},
        ])
        with patch.dict(os.environ, {"ARIS4U_EVENTS_LOG": str(log)}):
            counts = _read_session_signals_from_log("s3")
        assert counts["guard_blocks"] == 1

    def test_both_guard_types_combined(self, tmp_path: Path) -> None:
        log = tmp_path / "events.jsonl"
        _write_events(log, [
            {"event": "phi_to_external_blocked", "session_id": "s4"},
            {"event": "migration_lint_blocked", "session_id": "s4"},
            {"event": "phi_to_external_blocked", "session_id": "s4"},
        ])
        with patch.dict(os.environ, {"ARIS4U_EVENTS_LOG": str(log)}):
            counts = _read_session_signals_from_log("s4")
        assert counts["guard_blocks"] == 3

    def test_model_routing_blocked_counted(self, tmp_path: Path) -> None:
        """model_routing_blocked (frontier hook) counts as guard_block per session."""
        log = tmp_path / "events.jsonl"
        _write_events(log, [
            {"event": "model_routing_blocked", "session_id": "s5", "tool": "Agent"},
            {"event": "model_routing_blocked", "session_id": "s5", "tool": "Workflow"},
            {"event": "model_routing_blocked", "session_id": "OTHER", "tool": "Agent"},
        ])
        with patch.dict(os.environ, {"ARIS4U_EVENTS_LOG": str(log)}):
            counts = _read_session_signals_from_log("s5")
        assert counts["guard_blocks"] == 2

    def test_all_three_guard_types_combined(self, tmp_path: Path) -> None:
        """phi, migration_lint, and model_routing blocks all count together."""
        log = tmp_path / "events.jsonl"
        _write_events(log, [
            {"event": "phi_to_external_blocked", "session_id": "s6"},
            {"event": "migration_lint_blocked", "session_id": "s6"},
            {"event": "model_routing_blocked", "session_id": "s6", "tool": "Agent"},
            {"event": "model_routing_blocked", "session_id": "OTHER"},
        ])
        with patch.dict(os.environ, {"ARIS4U_EVENTS_LOG": str(log)}):
            counts = _read_session_signals_from_log("s6")
        assert counts["guard_blocks"] == 3

    def test_model_routing_guard_block_contributes_to_score(
        self, mem_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """A model_routing_blocked event from the frontier hook bumps the score."""
        log = tmp_path / "events.jsonl"
        # 1 guard_block, 0 recalls → score = 1 / max(0, 1) = 1.0
        _write_events(log, [
            {"event": "model_routing_blocked", "session_id": "s-frontier", "tool": "Agent"},
        ])
        with patch.dict(os.environ, {"ARIS4U_EVENTS_LOG": str(log)}):
            result = compute_amplification_score("s-frontier", db=mem_db)
        assert result["guard_blocks"] == 1
        assert result["score"] == pytest.approx(1.0)

    def test_missing_log_returns_zeros(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "nonexistent.jsonl")
        with patch.dict(os.environ, {"ARIS4U_EVENTS_LOG": missing}):
            counts = _read_session_signals_from_log("s-missing")
        assert counts["f1_total"] == 0
        assert counts["f1_useful"] == 0
        assert counts["guard_blocks"] == 0

    def test_f1_score_contributes_to_amplification(
        self, mem_db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """f1_useful bumps numerator; f1_total bumps denominator correctly."""
        log = tmp_path / "events.jsonl"
        _write_events(log, [
            {"event": "f1_feedback", "session_id": "s-f1", "useful": True, "call_id": "c1"},
            {"event": "f1_feedback", "session_id": "s-f1", "useful": False, "call_id": "c2"},
        ])
        with patch.dict(os.environ, {"ARIS4U_EVENTS_LOG": str(log)}):
            result = compute_amplification_score("s-f1", db=mem_db)
        # 1 useful f1 / max(0 recalls + 2 f1_total, 1) = 0.5
        assert result["f1_useful"] == 1
        assert result["f1_total"] == 2
        assert result["score"] == pytest.approx(0.5)
