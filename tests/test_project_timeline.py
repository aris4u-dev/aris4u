"""Tests for tools/project_timeline.py.

All tests are unit-level (no external services, no live sessions.db).
Each test uses either tmp_path or an in-memory / temp SQLite — never the
real data/sessions.db.

Schema used for the temp DB mirrors the REAL sessions.db schema discovered
2026-07-07:
  decisions:   id, digest_id, decision, rationale, domain, locked,
               session_ref, evidence, created_at, client_id, ...
  digests:     id (TEXT PK), date, session_id, summary, built, decisions,
               failed, guards, pending, tags, embedding, created_at, client_id
  gate_results: id, module_name, timestamp, status, details, e2e_prompt,
               session_ref, created_at

Anti-Goodhart: git is the anchor.  ARIS4U rows that cannot be correlated to
any commit within the temporal window are orphans and must NOT appear as
standalone timeline entries.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.project_timeline import (  # noqa: E402
    _parse_dt,
    add_comment,
    build_timeline,
    ensure_comments_table,
    list_comments,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    # Fixed dates so tests are deterministic.
    "GIT_AUTHOR_DATE": "2026-07-01T10:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-07-01T10:00:00+00:00",
}

_GIT_ENV_2 = {
    **_GIT_ENV,
    "GIT_AUTHOR_DATE": "2026-07-02T10:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-07-02T10:00:00+00:00",
}

_GIT_ENV_3 = {
    **_GIT_ENV,
    "GIT_AUTHOR_DATE": "2026-07-03T10:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-07-03T10:00:00+00:00",
}


def _git(args: list[str], cwd: Path, env: dict | None = None) -> None:
    """Run a git command, raising on failure."""
    subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env=env or _GIT_ENV,
    )


def _make_repo(tmp_path: Path) -> Path:
    """Initialise a git repo with 3 deterministic commits and return its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo, env=_GIT_ENV)
    _git(["config", "user.email", "test@example.com"], cwd=repo, env=_GIT_ENV)
    _git(["config", "user.name", "Test Author"], cwd=repo, env=_GIT_ENV)

    # Commit 1
    (repo / "alpha.py").write_text("# alpha\n")
    _git(["add", "alpha.py"], cwd=repo, env=_GIT_ENV)
    _git(["commit", "-m", "Add alpha module"], cwd=repo, env=_GIT_ENV)

    # Commit 2
    (repo / "beta.py").write_text("# beta\n")
    _git(["add", "beta.py"], cwd=repo, env=_GIT_ENV_2)
    _git(["commit", "-m", "Add beta module"], cwd=repo, env=_GIT_ENV_2)

    # Commit 3
    (repo / "gamma.py").write_text("# gamma\n")
    _git(["add", "gamma.py"], cwd=repo, env=_GIT_ENV_3)
    _git(["commit", "-m", "Add gamma module"], cwd=repo, env=_GIT_ENV_3)

    return repo


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal sessions.db with real schema, seeded with test data.

    Rows planted:
      decisions:
        - Row A  created_at 2026-07-01T10:30:00Z → inside commit-1 window
        - Row B  created_at 2026-07-10T00:00:00Z → ORPHAN (no commit near it)
      digests:
        - Row C  date 2026-07-02, created_at 2026-07-02T10:00:00Z → commit-2
        - Row D  date 2026-07-20, created_at 2026-07-20T00:00:00Z → ORPHAN
      gate_results:
        - Row E  timestamp 2026-07-03T10:00:00Z → commit-3
        - Row F  timestamp 2026-07-25T00:00:00Z → ORPHAN
    """
    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_id TEXT,
            decision TEXT NOT NULL,
            rationale TEXT,
            domain TEXT,
            locked INTEGER DEFAULT 0,
            session_ref TEXT,
            evidence TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            client_id TEXT DEFAULT NULL
        );

        CREATE TABLE digests (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            session_id TEXT,
            summary TEXT NOT NULL,
            built TEXT,
            decisions TEXT,
            failed TEXT,
            guards TEXT,
            pending TEXT,
            tags TEXT,
            embedding BLOB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            client_id TEXT DEFAULT NULL
        );

        CREATE TABLE gate_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module_name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            e2e_prompt TEXT,
            session_ref TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # decisions
    cur.execute(
        "INSERT INTO decisions (decision, rationale, domain, session_ref, created_at, client_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Use typed dicts", "Clarity", "arch", "sess-1", "2026-07-01T10:30:00", "aris4u"),
    )
    cur.execute(
        "INSERT INTO decisions (decision, rationale, domain, session_ref, created_at, client_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("Orphan decision", "Far future", "arch", "sess-X", "2026-07-10T00:00:00", "aris4u"),
    )

    # digests
    cur.execute(
        "INSERT INTO digests (id, date, summary, created_at, client_id) VALUES (?, ?, ?, ?, ?)",
        ("digest-c", "2026-07-02", "Beta shipping", "2026-07-02T10:00:00", "aris4u"),
    )
    cur.execute(
        "INSERT INTO digests (id, date, summary, created_at, client_id) VALUES (?, ?, ?, ?, ?)",
        ("digest-d", "2026-07-20", "Orphan digest", "2026-07-20T00:00:00", "aris4u"),
    )

    # gate_results
    cur.execute(
        "INSERT INTO gate_results (module_name, timestamp, status, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("gamma_gate", "2026-07-03T10:00:00", "PASS", "2026-07-03T10:00:00"),
    )
    cur.execute(
        "INSERT INTO gate_results (module_name, timestamp, status, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("orphan_gate", "2026-07-25T00:00:00", "PASS", "2026-07-25T00:00:00"),
    )

    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_build_timeline_returns_commits_in_order(tmp_path: Path) -> None:
    """build_timeline returns one entry per commit, most-recent first (git log order)."""
    repo = _make_repo(tmp_path)
    db = _make_db(tmp_path)

    timeline = build_timeline(repo_path=repo, client_id="aris4u", db_path=db)

    assert len(timeline) == 3
    # git log is newest-first.
    assert timeline[0]["subject"] == "Add gamma module"
    assert timeline[1]["subject"] == "Add beta module"
    assert timeline[2]["subject"] == "Add alpha module"

    # Each entry has the expected keys.
    for entry in timeline:
        assert "sha" in entry
        assert "author" in entry
        assert "date" in entry
        assert "files" in entry
        assert "why" in entry
        assert set(entry["why"]) == {"decisions", "digests", "gates"}


def test_correlated_commit_has_why_populated(tmp_path: Path) -> None:
    """Commits within window of an ARIS4U row get that row in their why."""
    repo = _make_repo(tmp_path)
    db = _make_db(tmp_path)

    timeline = build_timeline(repo_path=repo, client_id="aris4u", db_path=db)

    # Commit 3 (gamma, 2026-07-03) should have the gate_results row E.
    gamma = next(e for e in timeline if e["subject"] == "Add gamma module")
    assert len(gamma["why"]["gates"]) == 1
    assert gamma["why"]["gates"][0]["module_name"] == "gamma_gate"

    # Commit 1 (alpha, 2026-07-01) should have decision row A.
    alpha = next(e for e in timeline if e["subject"] == "Add alpha module")
    assert len(alpha["why"]["decisions"]) == 1
    assert alpha["why"]["decisions"][0]["decision"] == "Use typed dicts"

    # Commit 2 (beta, 2026-07-02) should have digest row C.
    beta = next(e for e in timeline if e["subject"] == "Add beta module")
    assert len(beta["why"]["digests"]) == 1
    assert beta["why"]["digests"][0]["summary"] == "Beta shipping"


def test_commit_without_aris4u_row_has_empty_why(tmp_path: Path) -> None:
    """Commits with no matching ARIS4U rows appear with empty why dicts — never omitted."""
    repo = _make_repo(tmp_path)
    # Empty DB — no ARIS4U rows at all.
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE decisions (id INTEGER PRIMARY KEY, decision TEXT,
            rationale TEXT, domain TEXT,
            session_ref TEXT, created_at TEXT, client_id TEXT);
        CREATE TABLE digests (id TEXT PRIMARY KEY, date TEXT, summary TEXT,
            built TEXT, session_id TEXT, created_at TEXT, client_id TEXT);
        CREATE TABLE gate_results (id INTEGER PRIMARY KEY, module_name TEXT,
            timestamp TEXT, status TEXT, details TEXT, session_ref TEXT,
            created_at TEXT);
        """
    )
    conn.commit()
    conn.close()

    timeline = build_timeline(repo_path=repo, client_id="aris4u", db_path=db)

    assert len(timeline) == 3
    for entry in timeline:
        assert entry["why"]["decisions"] == []
        assert entry["why"]["digests"] == []
        assert entry["why"]["gates"] == []


def test_orphan_aris4u_rows_not_in_timeline(tmp_path: Path) -> None:
    """Anti-Goodhart: orphan ARIS4U rows (no nearby commit) generate NO timeline entries."""
    repo = _make_repo(tmp_path)
    db = _make_db(tmp_path)

    timeline = build_timeline(repo_path=repo, client_id="aris4u", db_path=db)

    # Total entries must be exactly the number of commits (3), not more.
    assert len(timeline) == 3

    # Orphan rows must not appear anywhere in any why block.
    all_decision_texts = [
        d["decision"]
        for entry in timeline
        for d in entry["why"]["decisions"]
    ]
    assert "Orphan decision" not in all_decision_texts

    all_digest_summaries = [
        d["summary"]
        for entry in timeline
        for d in entry["why"]["digests"]
    ]
    assert "Orphan digest" not in all_digest_summaries

    all_gate_names = [
        g["module_name"]
        for entry in timeline
        for g in entry["why"]["gates"]
    ]
    assert "orphan_gate" not in all_gate_names


def test_add_and_list_comments_roundtrip(tmp_path: Path) -> None:
    """add_comment then list_comments returns the same data."""
    db = tmp_path / "comments_test.db"
    sha = "abc123def456"

    row_id = add_comment(
        db_path=db,
        commit_sha=sha,
        author="user-a",
        role="reviewer",
        body="Looks good — anti-Goodhart holds.",
        client_id="aris4u",
    )
    assert isinstance(row_id, int)
    assert row_id >= 1

    comments = list_comments(db_path=db, commit_sha=sha)
    assert len(comments) == 1
    c = comments[0]
    assert c["commit_sha"] == sha
    assert c["author"] == "user-a"
    assert c["role"] == "reviewer"
    assert c["body"] == "Looks good — anti-Goodhart holds."
    assert c["client_id"] == "aris4u"
    assert c["id"] == row_id

    # Different SHA returns empty list.
    assert list_comments(db_path=db, commit_sha="000000") == []


def test_ensure_comments_table_is_idempotent(tmp_path: Path) -> None:
    """ensure_comments_table can be called multiple times without error or schema duplication."""
    db = tmp_path / "idempotent.db"

    ensure_comments_table(db)
    ensure_comments_table(db)  # Second call must not raise.
    ensure_comments_table(db)  # Third for good measure.

    # Verify the table exists and has exactly one definition.
    conn = sqlite3.connect(str(db))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='cowork_comments'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1, "Table must exist exactly once after idempotent calls."


def test_empty_repo_returns_empty_timeline(tmp_path: Path) -> None:
    """A repo with no commits returns an empty timeline without crashing."""
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo, env=_GIT_ENV)
    _git(["config", "user.email", "test@example.com"], cwd=repo, env=_GIT_ENV)
    _git(["config", "user.name", "Test Author"], cwd=repo, env=_GIT_ENV)

    db = tmp_path / "any.db"

    result = build_timeline(repo_path=repo, client_id="aris4u", db_path=db)
    assert result == []


def test_nonexistent_sha_in_comments_returns_empty(tmp_path: Path) -> None:
    """list_comments on an SHA that has no comments returns an empty list, no crash."""
    db = tmp_path / "no_comments.db"
    ensure_comments_table(db)

    result = list_comments(db_path=db, commit_sha="deadbeef")
    assert result == []


def test_build_timeline_no_db_returns_commits_with_empty_why(tmp_path: Path) -> None:
    """When sessions.db does not exist, commits are returned with empty why dicts."""
    repo = _make_repo(tmp_path)
    absent_db = tmp_path / "does_not_exist.db"

    timeline = build_timeline(repo_path=repo, client_id="aris4u", db_path=absent_db)

    assert len(timeline) == 3
    for entry in timeline:
        assert entry["why"] == {"decisions": [], "digests": [], "gates": []}


def test_parse_dt_handles_various_formats() -> None:
    """_parse_dt correctly parses ISO-8601 strings with and without offsets."""
    dt_with_tz = _parse_dt("2026-07-01T10:00:00+00:00")
    assert dt_with_tz is not None
    assert dt_with_tz.year == 2026

    # Microseconds + offset — previously broken by [:26] slice.
    dt_us = _parse_dt("2026-07-03T10:04:18.951721+00:00")
    assert dt_us is not None
    assert dt_us.microsecond == 951721

    dt_date_only = _parse_dt("2026-07-01")
    assert dt_date_only is not None
    assert dt_date_only.month == 7

    # SQLite space-separator format.
    dt_space = _parse_dt("2026-07-01 12:34:56")
    assert dt_space is not None
    assert dt_space.hour == 12

    assert _parse_dt(None) is None
    assert _parse_dt("") is None
    assert _parse_dt("not-a-date") is None


# ---------------------------------------------------------------------------
# New tests for gate findings
# ---------------------------------------------------------------------------


def test_build_timeline_db_without_tables_returns_empty_why(tmp_path: Path) -> None:
    """P0-1: DB exists but has no ARIS4U tables → no crash, commits with empty why."""
    repo = _make_repo(tmp_path)
    # Create a valid SQLite file with NO tables at all.
    bare_db = tmp_path / "bare.db"
    conn = sqlite3.connect(str(bare_db))
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    # Must not raise OperationalError — degrades to empty why.
    timeline = build_timeline(repo_path=repo, client_id="aris4u", db_path=bare_db)

    assert len(timeline) == 3
    for entry in timeline:
        assert entry["why"] == {"decisions": [], "digests": [], "gates": []}


def test_list_comments_on_db_without_table_returns_empty(tmp_path: Path) -> None:
    """P0-2: list_comments on a DB that has no cowork_comments table returns [] without creating anything."""
    # DB exists but has no cowork_comments table.
    db = tmp_path / "no_table.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    mtime_before = db.stat().st_mtime

    result = list_comments(db_path=db, commit_sha="abc123")
    assert result == []

    # The DB must not have been modified (no table created, no write).
    mtime_after = db.stat().st_mtime
    assert mtime_after == mtime_before, "list_comments must not write to the DB"

    # Verify cowork_comments was NOT created.
    conn2 = sqlite3.connect(str(db))
    tables = {r[0] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn2.close()
    assert "cowork_comments" not in tables


def test_list_comments_on_nonexistent_db_returns_empty(tmp_path: Path) -> None:
    """P0-2: list_comments on a path that does not exist returns [] without creating the file."""
    absent = tmp_path / "ghost.db"
    result = list_comments(db_path=absent, commit_sha="abc123")
    assert result == []
    assert not absent.exists(), "list_comments must not create the DB file"


def test_closest_commit_wins_in_dense_session(tmp_path: Path) -> None:
    """P1-2: when multiple commits fall in the window, each row goes to its closest commit.

    Layout (all within a 6-hour window of each other):
      Commit A  2026-07-05T10:00:00Z
      Commit B  2026-07-05T12:00:00Z
      Commit C  2026-07-05T14:00:00Z

    ARIS4U rows:
      Decision-1  created_at 2026-07-05T10:05:00Z  → closest to Commit A (5 min)
      Decision-2  created_at 2026-07-05T12:02:00Z  → closest to Commit B (2 min)
      Gate-1      timestamp  2026-07-05T13:55:00Z  → closest to Commit C (5 min)
    """
    repo = tmp_path / "dense_repo"
    repo.mkdir()
    _git(["init", "-b", "main"], cwd=repo, env=_GIT_ENV)
    _git(["config", "user.email", "test@example.com"], cwd=repo, env=_GIT_ENV)
    _git(["config", "user.name", "Test Author"], cwd=repo, env=_GIT_ENV)

    env_a = {**_GIT_ENV,
              "GIT_AUTHOR_DATE": "2026-07-05T10:00:00+00:00",
              "GIT_COMMITTER_DATE": "2026-07-05T10:00:00+00:00"}
    env_b = {**_GIT_ENV,
              "GIT_AUTHOR_DATE": "2026-07-05T12:00:00+00:00",
              "GIT_COMMITTER_DATE": "2026-07-05T12:00:00+00:00"}
    env_c = {**_GIT_ENV,
              "GIT_AUTHOR_DATE": "2026-07-05T14:00:00+00:00",
              "GIT_COMMITTER_DATE": "2026-07-05T14:00:00+00:00"}

    (repo / "a.py").write_text("# a\n")
    _git(["add", "a.py"], cwd=repo, env=env_a)
    _git(["commit", "-m", "Commit A"], cwd=repo, env=env_a)

    (repo / "b.py").write_text("# b\n")
    _git(["add", "b.py"], cwd=repo, env=env_b)
    _git(["commit", "-m", "Commit B"], cwd=repo, env=env_b)

    (repo / "c.py").write_text("# c\n")
    _git(["add", "c.py"], cwd=repo, env=env_c)
    _git(["commit", "-m", "Commit C"], cwd=repo, env=env_c)

    db = tmp_path / "dense.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision TEXT NOT NULL,
            rationale TEXT,
            domain TEXT,
            session_ref TEXT,
            created_at TEXT,
            client_id TEXT
        );
        CREATE TABLE digests (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            summary TEXT NOT NULL,
            built TEXT,
            session_id TEXT,
            created_at TEXT,
            client_id TEXT
        );
        CREATE TABLE gate_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module_name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            session_ref TEXT,
            created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO decisions (decision, created_at, client_id) VALUES (?, ?, ?)",
        ("Decision-1", "2026-07-05T10:05:00", "aris4u"),
    )
    conn.execute(
        "INSERT INTO decisions (decision, created_at, client_id) VALUES (?, ?, ?)",
        ("Decision-2", "2026-07-05T12:02:00", "aris4u"),
    )
    conn.execute(
        "INSERT INTO gate_results (module_name, timestamp, status, created_at) VALUES (?, ?, ?, ?)",
        ("gate-c", "2026-07-05T13:55:00", "PASS", "2026-07-05T13:55:00"),
    )
    conn.commit()
    conn.close()

    timeline = build_timeline(repo_path=repo, client_id="aris4u", db_path=db)

    commit_a = next(e for e in timeline if e["subject"] == "Commit A")
    commit_b = next(e for e in timeline if e["subject"] == "Commit B")
    commit_c = next(e for e in timeline if e["subject"] == "Commit C")

    # Decision-1 is 5 min from A, 1h55 from B → goes to A.
    assert len(commit_a["why"]["decisions"]) == 1
    assert commit_a["why"]["decisions"][0]["decision"] == "Decision-1"

    # Decision-2 is 2 min from B, 1h58 from A → goes to B.
    assert len(commit_b["why"]["decisions"]) == 1
    assert commit_b["why"]["decisions"][0]["decision"] == "Decision-2"

    # Gate-1 is 5 min from C, 1h55 from B → goes to C.
    assert len(commit_c["why"]["gates"]) == 1
    assert commit_c["why"]["gates"][0]["module_name"] == "gate-c"

    # Anti-Goodhart: total timeline entries = number of commits, not more.
    assert len(timeline) == 3
