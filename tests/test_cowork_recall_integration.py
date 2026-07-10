"""Increment 4 — cowork_comments round-trip via aris_recall_client.

Tests exercise:
  1. _recall_cowork helper directly (temp DB, no live sessions.db writes).
  2. Round-trip: seed cowork_comments → aris_recall_client returns the comment
     in a dedicated section with sha_short and author.
  3. Isolation: a comment scoped to client B never appears in client A's recall.
  4. guard_only tier (cowork_limit=0) suppresses the section entirely.
  5. Empty result when no comments exist for the client.

All tests use sqlite3 in-memory or tmp_path DBs — never data/sessions.db.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Availability guards
# ---------------------------------------------------------------------------

# tools.project_timeline is a pure-stdlib local module — always resolvable in the
# venv; import directly so Pyright sees the names as unconditionally bound.
from tools.project_timeline import add_comment, ensure_comments_table  # noqa: E402

# integrations.mcp_server requires the `mcp` package.  pytest.importorskip raises
# Skipped at collection time when the module is absent, so from Pyright's perspective
# the import below is always reachable — names are unconditionally bound.
pytest.importorskip("mcp", reason="mcp package not installed")
pytest.importorskip("integrations.mcp_server", reason="MCP stack not available")

from integrations.mcp_server import _recall_cowork, aris_recall_client  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cowork_db(tmp_path: Path) -> Path:  # noqa: D103
    """Return a temp sessions.db with cowork_comments seeded for two clients."""
    db_path = tmp_path / "sessions.db"
    ensure_comments_table(db_path)
    add_comment(
        db_path=db_path,
        commit_sha="abc1234567890",
        author="reviewer",
        role="qa",
        body="This auth flow needs rate-limiting.",
        client_id="testclient",
    )
    add_comment(
        db_path=db_path,
        commit_sha="def9876543210",
        author="user-a",
        role="lead",
        body="Confidential feedback for other-client.",
        client_id="other-client",
    )
    return db_path


def _open_rw(db_path: Path) -> sqlite3.Connection:  # noqa: D103
    """Open a read-write connection with row_factory set (mirrors session_manager._connect)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Unit tests for _recall_cowork helper
# ---------------------------------------------------------------------------

def test_recall_cowork_returns_comment_for_correct_client(tmp_path: Path) -> None:
    """_recall_cowork returns formatted lines for the matching client_id."""
    db_path = _make_cowork_db(tmp_path)
    conn = _open_rw(db_path)
    try:
        lines = _recall_cowork(conn, "testclient", limit=5)
    finally:
        conn.close()

    assert len(lines) == 1
    assert "abc1234" in lines[0]          # sha_short (first 7 chars)
    assert "reviewer" in lines[0]
    assert "qa" in lines[0]
    assert "rate-limiting" in lines[0]


def test_recall_cowork_isolation_between_clients(tmp_path: Path) -> None:
    """_recall_cowork never leaks rows from a different client_id."""
    db_path = _make_cowork_db(tmp_path)
    conn = _open_rw(db_path)
    try:
        lines_a = _recall_cowork(conn, "testclient", limit=5)
        lines_b = _recall_cowork(conn, "other-client", limit=5)
    finally:
        conn.close()

    # testclient only sees its own comment
    assert all("Confidential" not in line for line in lines_a)
    # other-client only sees its own comment
    assert all("rate-limiting" not in line for line in lines_b)
    assert len(lines_b) == 1
    assert "user-a" in lines_b[0]


def test_recall_cowork_empty_when_no_comments(tmp_path: Path) -> None:
    """_recall_cowork returns [] when client has no comments."""
    db_path = _make_cowork_db(tmp_path)
    conn = _open_rw(db_path)
    try:
        lines = _recall_cowork(conn, "unknown-client", limit=5)
    finally:
        conn.close()

    assert lines == []


def test_recall_cowork_respects_limit(tmp_path: Path) -> None:
    """_recall_cowork honours the limit parameter."""
    db_path = tmp_path / "sessions.db"
    ensure_comments_table(db_path)
    for i in range(4):
        add_comment(
            db_path=db_path,
            commit_sha=f"sha{i:040d}",
            author="dev",
            role="dev",
            body=f"comment {i}",
            client_id="limitclient",
        )
    conn = _open_rw(db_path)
    try:
        lines = _recall_cowork(conn, "limitclient", limit=2)
    finally:
        conn.close()

    assert len(lines) == 2


def test_recall_cowork_empty_sha_handled(tmp_path: Path) -> None:
    """_recall_cowork formats rows with an empty-string commit_sha as 'unknown'."""
    db_path = tmp_path / "sessions.db"
    ensure_comments_table(db_path)
    # Empty string sha exercises the (row[0] or "unknown")[:7] fallback branch.
    add_comment(
        db_path=db_path,
        commit_sha="",
        author="tester",
        role="qa",
        body="empty sha comment",
        client_id="emptyshalient",
    )

    conn = _open_rw(db_path)
    try:
        lines = _recall_cowork(conn, "emptyshalient", limit=5)
    finally:
        conn.close()

    assert len(lines) == 1
    assert "unknown" in lines[0]
    assert "empty sha comment" in lines[0]


# ---------------------------------------------------------------------------
# Integration: aris_recall_client round-trip via monkeypatched SESSIONS_DB
# ---------------------------------------------------------------------------

def test_aris_recall_client_cowork_section_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round-trip: seed cowork_comments → aris_recall_client includes Cowork Feedback section."""
    db_path = _make_cowork_db(tmp_path)

    # Redirect SESSIONS_DB and session_manager to the temp DB.
    import integrations.mcp_server as mcp_mod
    import engine.v16.session_manager as sm_mod
    import engine.v16.config as cfg_mod

    monkeypatch.setattr(mcp_mod, "SESSIONS_DB", db_path)
    monkeypatch.setattr(cfg_mod, "SESSIONS_DB", db_path)

    # Patch session_manager._connect to use the temp DB.
    def _fake_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(sm_mod, "_connect", _fake_connect)

    # Also patch semantic_recall to return [] (no vector sidecar in temp DB).
    monkeypatch.setattr(sm_mod, "semantic_recall", lambda *_, **__: [])

    out = aris_recall_client("testclient")

    assert "=== testclient Cowork Feedback ===" in out
    assert "abc1234" in out       # sha_short
    assert "reviewer" in out
    assert "rate-limiting" in out


def test_aris_recall_client_cowork_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """aris_recall_client for testclient must NOT include other-client's feedback."""
    db_path = _make_cowork_db(tmp_path)

    import integrations.mcp_server as mcp_mod
    import engine.v16.session_manager as sm_mod
    import engine.v16.config as cfg_mod

    monkeypatch.setattr(mcp_mod, "SESSIONS_DB", db_path)
    monkeypatch.setattr(cfg_mod, "SESSIONS_DB", db_path)

    def _fake_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(sm_mod, "_connect", _fake_connect)
    monkeypatch.setattr(sm_mod, "semantic_recall", lambda *_, **__: [])

    out = aris_recall_client("testclient")

    assert "Confidential feedback for other-client" not in out


def test_aris_recall_client_guard_only_tier_omits_cowork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """guard_only tier sets cowork_limit=0 → Cowork Feedback section is absent."""
    db_path = _make_cowork_db(tmp_path)

    import integrations.mcp_server as mcp_mod
    import engine.v16.session_manager as sm_mod
    import engine.v16.config as cfg_mod

    monkeypatch.setattr(mcp_mod, "SESSIONS_DB", db_path)
    monkeypatch.setattr(cfg_mod, "SESSIONS_DB", db_path)

    def _fake_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(sm_mod, "_connect", _fake_connect)
    monkeypatch.setattr(sm_mod, "semantic_recall", lambda *_, **__: [])

    out = aris_recall_client("testclient", tier="guard_only")

    assert "Cowork Feedback" not in out


def test_aris_recall_client_no_cowork_section_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When client has no cowork comments the section header is omitted entirely."""
    db_path = tmp_path / "sessions.db"
    ensure_comments_table(db_path)  # table exists but empty

    import integrations.mcp_server as mcp_mod
    import engine.v16.session_manager as sm_mod
    import engine.v16.config as cfg_mod

    monkeypatch.setattr(mcp_mod, "SESSIONS_DB", db_path)
    monkeypatch.setattr(cfg_mod, "SESSIONS_DB", db_path)

    def _fake_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(sm_mod, "_connect", _fake_connect)
    monkeypatch.setattr(sm_mod, "semantic_recall", lambda *_, **__: [])

    out = aris_recall_client("emptyclient")

    assert "Cowork Feedback" not in out


def test_ensure_comments_table_called_at_most_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_COWORK_TABLE_READY flag ensures ensure_comments_table is called once per process.

    We reset the flag to False before the test so it runs from a cold-start state,
    then verify ensure_comments_table is called exactly once across two consecutive
    aris_recall_client calls.
    """
    db_path = _make_cowork_db(tmp_path)

    import integrations.mcp_server as mcp_mod
    import engine.v16.session_manager as sm_mod
    import engine.v16.config as cfg_mod

    monkeypatch.setattr(mcp_mod, "SESSIONS_DB", db_path)
    monkeypatch.setattr(cfg_mod, "SESSIONS_DB", db_path)
    # Reset the flag so this test always starts from a cold-process state.
    monkeypatch.setattr(mcp_mod, "_COWORK_TABLE_READY", False)

    def _fake_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(sm_mod, "_connect", _fake_connect)
    monkeypatch.setattr(sm_mod, "semantic_recall", lambda *_, **__: [])

    call_count = 0

    def _counting_ensure(path: Path | str) -> None:
        nonlocal call_count
        call_count += 1
        ensure_comments_table(path)  # still do the real work

    monkeypatch.setattr(mcp_mod, "ensure_comments_table", _counting_ensure)

    # First call: flag is False → ensure_comments_table should fire once.
    aris_recall_client("testclient")
    assert call_count == 1, "expected exactly 1 call on first invocation"
    assert mcp_mod._COWORK_TABLE_READY is True

    # Second call: flag is now True → ensure_comments_table must NOT fire again.
    aris_recall_client("testclient")
    assert call_count == 1, "ensure_comments_table must not be called on repeat invocations"
