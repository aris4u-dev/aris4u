"""
FTS5 tokenizer hyphenated terms verification.

W3.6 investigation: FTS5 MATCH syntax interprets hyphen as NOT operator,
not tokenizer issue. Quoted phrases work; bare hyphens fail.
"""

import sqlite3
import tempfile
import shutil
from pathlib import Path

import pytest

# Todo el módulo requiere ~/.claude-mem/claude-mem.db REAL y poblada (≥100 obs):
# es validación del substrato FTS5 vivo, no un unit test. Se deselecciona en CI.
pytestmark = pytest.mark.integration


def test_fts5_hyphenated_query_quoted_phrase():
    """Test that quoted phrases work for hyphenated terms."""
    db_path = Path.home() / ".claude-mem" / "claude-mem.db"
    assert db_path.exists(), f"Database not found: {db_path}"

    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Path(tmpdir) / "test_claude_mem.db"
        shutil.copy(db_path, test_db)

        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()

        # Test 1: quoted phrase query "claude-mem" succeeds
        result = cursor.execute(
            'SELECT COUNT(*) FROM observations_fts WHERE observations_fts MATCH \'"claude-mem"\''
        ).fetchone()
        assert result[0] >= 1, "Quoted phrase 'claude-mem' should return >= 1 result"
        quoted_count = result[0]

        conn.close()

    # Assert: quoted phrase returned meaningful count (corpus grows over time, use lower bound)
    assert quoted_count >= 100, f"Expected >=100 results for quoted 'claude-mem', got {quoted_count}"


def test_fts5_unquoted_hyphenated_query_fails():
    """Test that unquoted hyphenated queries fail with 'no such column' error."""
    db_path = Path.home() / ".claude-mem" / "claude-mem.db"
    assert db_path.exists(), f"Database not found: {db_path}"

    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Path(tmpdir) / "test_claude_mem.db"
        shutil.copy(db_path, test_db)

        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()

        # Test 2: unquoted hyphenated query fails
        try:
            cursor.execute(
                'SELECT COUNT(*) FROM observations_fts WHERE observations_fts MATCH \'claude-mem\''
            ).fetchone()
            assert False, "Unquoted 'claude-mem' should raise error"
        except sqlite3.Error as e:
            assert "no such column" in str(e), f"Expected 'no such column' error, got: {e}"

        conn.close()


def test_fts5_non_hyphenated_queries_work():
    """Test that non-hyphenated queries work correctly."""
    db_path = Path.home() / ".claude-mem" / "claude-mem.db"
    assert db_path.exists(), f"Database not found: {db_path}"

    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Path(tmpdir) / "test_claude_mem.db"
        shutil.copy(db_path, test_db)

        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()

        # Test 3: non-hyphenated compound query works
        result = cursor.execute(
            'SELECT COUNT(*) FROM observations_fts WHERE observations_fts MATCH \'memory database\''
        ).fetchone()
        assert result[0] >= 1, "Query 'memory database' should return >= 1 result"
        non_hyphen_count = result[0]

        conn.close()

    assert non_hyphen_count >= 50, f"Expected >=50 results for 'memory database', got {non_hyphen_count}"


def test_fts5_single_terms_work():
    """Test that single term queries work."""
    db_path = Path.home() / ".claude-mem" / "claude-mem.db"
    assert db_path.exists(), f"Database not found: {db_path}"

    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Path(tmpdir) / "test_claude_mem.db"
        shutil.copy(db_path, test_db)

        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()

        # Test 4: single term queries work
        result = cursor.execute(
            'SELECT COUNT(*) FROM observations_fts WHERE observations_fts MATCH \'memory\''
        ).fetchone()
        assert result[0] >= 1, "Query 'memory' should return >= 1 result"
        single_term_count = result[0]

        conn.close()

    assert single_term_count > 100, f"Expected >100 results for 'memory', got {single_term_count}"


def test_fts5_integrity_check():
    """Test that database integrity is preserved."""
    db_path = Path.home() / ".claude-mem" / "claude-mem.db"
    assert db_path.exists(), f"Database not found: {db_path}"

    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Path(tmpdir) / "test_claude_mem.db"
        shutil.copy(db_path, test_db)

        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()

        # Test 5: integrity check passes
        result = cursor.execute('PRAGMA integrity_check').fetchone()
        assert result[0] == "ok", f"Integrity check failed: {result[0]}"

        # Verify observations table count
        count = cursor.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        assert count >= 7000, f"Expected >=7000 observations, got {count}"

        conn.close()


def test_fts5_observation_count_matches():
    """Test that FTS5 content table has matching row count."""
    db_path = Path.home() / ".claude-mem" / "claude-mem.db"
    assert db_path.exists(), f"Database not found: {db_path}"

    with tempfile.TemporaryDirectory() as tmpdir:
        test_db = Path(tmpdir) / "test_claude_mem.db"
        shutil.copy(db_path, test_db)

        conn = sqlite3.connect(test_db)
        cursor = conn.cursor()

        obs_count = cursor.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        fts_count = cursor.execute('SELECT COUNT(*) FROM observations_fts').fetchone()[0]

        assert obs_count == fts_count, f"observations ({obs_count}) != observations_fts ({fts_count})"

        conn.close()
