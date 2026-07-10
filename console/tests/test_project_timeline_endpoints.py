"""Tests for /project, /project/stream, /project/comment endpoints.

Style follows test_server.py (direct unit-test of handler functions) and
test_manifest_complete.py (import from aris4u_console.server directly).
All DB access uses isolated tmp_path fixtures — the live data/sessions.db
is never touched.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import MagicMock

# Ensure the console package (repo/console/) is importable.
_CONSOLE_ROOT = Path(__file__).parent.parent
if str(_CONSOLE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONSOLE_ROOT))

from aris4u_console import server  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal git repo fixture
# ---------------------------------------------------------------------------

def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one commit so build_timeline returns data."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(repo), capture_output=True)
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"],
                   cwd=str(repo), capture_output=True)
    return repo


def _make_db(tmp_path: Path, client_repo: Path | None = None,
             client_id: str = "aris4u") -> Path:
    """Create a minimal sessions.db with expected tables (no data needed).

    If ``client_repo`` is given, also seeds a build_run row for ``client_id``
    pointing at that repo — required now that _project_timeline uses
    repo_for_client() to resolve which repo to read from.
    """
    db = tmp_path / "sessions.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS decisions "
        "(id INTEGER PRIMARY KEY, decision TEXT, rationale TEXT, domain TEXT, "
        " session_ref TEXT, created_at TEXT, client_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS digests "
        "(id INTEGER PRIMARY KEY, date TEXT, summary TEXT, built INTEGER, "
        " session_id TEXT, created_at TEXT, client_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gate_results "
        "(id INTEGER PRIMARY KEY, module_name TEXT, status TEXT, details TEXT, "
        " session_ref TEXT, timestamp TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS build_runs "
        "(run_id INTEGER PRIMARY KEY, intake_id INTEGER NOT NULL, "
        " client_id TEXT NOT NULL, repo_path TEXT NOT NULL, "
        " log_path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running', "
        " started_at TEXT NOT NULL, ended_at TEXT)"
    )
    conn.commit()
    if client_repo is not None:
        conn.execute(
            "INSERT INTO build_runs "
            "(intake_id, client_id, repo_path, log_path, status, started_at) "
            "VALUES (1, ?, ?, '/tmp/build.log', 'done', '2026-07-07T00:00:00Z')",
            (client_id, str(client_repo)),
        )
        conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Fake HTTP handler infrastructure (no real socket needed)
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Captures what Handler._send / send_response / send_header / end_headers write."""

    def __init__(self) -> None:
        self.status: int = 0
        self.headers: dict[str, str] = {}
        self.body: bytes = b""
        self._buf = io.BytesIO()

    def write(self, data: bytes) -> None:
        self._buf.write(data)

    def flush(self) -> None:
        pass


class _HandlerFixture(NamedTuple):
    """Pair returned by _make_handler: the monkey-patched handler and its fake response."""
    handler: Any  # dynamically created inner class; no importable concrete type
    resp: _FakeResponse


def _make_handler(repo: Path, db: Path) -> _HandlerFixture:
    """Return a Handler instance wired to a fake socket/response for the given repo+db.

    ``handler`` is typed as ``Any`` because it is created via ``__new__`` on a dynamically
    generated inner class (the closure returned by ``server._make_handler``).  There is no
    importable concrete type for it; typing it as ``BaseHTTPRequestHandler`` would require
    ``# type: ignore`` on every attribute assignment that differs from the base class
    contract (wfile, headers, monkey-patched methods).  ``Any`` is the honest annotation
    here: we deliberately bypass ``__init__`` and patch the instance for testing.
    """
    out_dir = repo / "out"
    out_dir.mkdir(exist_ok=True)
    HandlerClass = server._make_handler(repo, out_dir)

    resp = _FakeResponse()

    # h is Any: dynamically created inner class, bypassing __init__.
    h: Any = HandlerClass.__new__(HandlerClass)
    h.wfile = resp
    h.headers = {}
    h.server = MagicMock()

    # Patch _send to capture status + body.
    def fake_send(code: int, body: bytes, _: str) -> None:
        resp.status = code
        resp.body = body

    h._send = fake_send

    # Patch _json to capture JSON responses.
    def fake_json(obj: dict) -> None:
        resp.status = 200
        resp.body = json.dumps(obj, ensure_ascii=False).encode()

    h._json = fake_json

    # Override _sessions_db_path to use the isolated fixture db.
    def fake_db() -> Path:
        return db

    h._sessions_db_path = fake_db

    return _HandlerFixture(handler=h, resp=resp)


# ---------------------------------------------------------------------------
# Tests: GET /project
# ---------------------------------------------------------------------------

def test_get_project_requires_client(tmp_path: Path) -> None:
    """GET /project without ?client= must return 400."""
    repo = _make_git_repo(tmp_path)
    db = _make_db(tmp_path)
    fixture = _make_handler(repo, db)

    # Simulate _origin_ok = True (curl-style, no browser origin).
    fixture.handler._origin_ok = lambda: True
    fixture.handler._project_timeline("")  # empty query string
    assert fixture.resp.status == 400
    data = json.loads(fixture.resp.body)
    assert "client" in data["error"].lower()


def test_get_project_returns_json_timeline(tmp_path: Path) -> None:
    """GET /project?client=aris4u returns valid JSON with timeline array."""
    repo = _make_git_repo(tmp_path)
    # Seed build_run so repo_for_client resolves to this repo (FIX 1).
    db = _make_db(tmp_path, client_repo=repo, client_id="aris4u")
    fixture = _make_handler(repo, db)

    fixture.handler._origin_ok = lambda: True
    fixture.handler._project_timeline("client=aris4u")
    assert fixture.resp.status == 200
    data = json.loads(fixture.resp.body)
    assert data.get("available") is True
    assert data.get("client") == "aris4u"
    assert isinstance(data.get("timeline"), list)
    # The repo has one commit → at least one entry.
    assert data["count"] >= 1


def test_get_project_structure(tmp_path: Path) -> None:
    """Each timeline entry has the expected keys."""
    repo = _make_git_repo(tmp_path)
    # Seed build_run so repo_for_client resolves to this repo (FIX 1).
    db = _make_db(tmp_path, client_repo=repo, client_id="aris4u")
    fixture = _make_handler(repo, db)

    fixture.handler._origin_ok = lambda: True
    fixture.handler._project_timeline("client=aris4u")
    data = json.loads(fixture.resp.body)
    entry = data["timeline"][0]
    for key in ("sha", "author", "date", "subject", "files", "why"):
        assert key in entry, f"missing key: {key}"
    why = entry["why"]
    for key in ("decisions", "digests", "gates"):
        assert key in why, f"missing why key: {key}"


def test_get_project_in_sensitive_gets() -> None:
    """/project and /project/stream are in _SENSITIVE_GETS (cross-origin guard)."""
    assert "/project" in server._SENSITIVE_GETS
    assert "/project/stream" in server._SENSITIVE_GETS


# ---------------------------------------------------------------------------
# Tests: POST /project/comment
# ---------------------------------------------------------------------------

def test_post_comment_inserts_and_returns_id(tmp_path: Path) -> None:
    """POST /project/comment with valid body inserts the row and returns {ok, id}."""
    repo = _make_git_repo(tmp_path)
    db = _make_db(tmp_path)
    fixture = _make_handler(repo, db)

    body = {
        "sha": "abc123",
        "author": "user-a",
        "role": "dev",
        "body": "looks good",
        "client": "aris4u",
    }
    fixture.handler._project_comment(body)
    assert fixture.resp.status == 200
    data = json.loads(fixture.resp.body)
    assert data["ok"] is True
    assert isinstance(data["id"], int) and data["id"] > 0


def test_post_comment_rejects_missing_sha(tmp_path: Path) -> None:
    """POST /project/comment without sha must return 400."""
    repo = _make_git_repo(tmp_path)
    db = _make_db(tmp_path)
    fixture = _make_handler(repo, db)

    fixture.handler._project_comment(
        {"body": "hello", "author": "x", "role": "dev", "client": "aris4u"}
    )
    assert fixture.resp.status == 400


def test_post_comment_rejects_missing_body(tmp_path: Path) -> None:
    """POST /project/comment without body text must return 400."""
    repo = _make_git_repo(tmp_path)
    db = _make_db(tmp_path)
    fixture = _make_handler(repo, db)

    fixture.handler._project_comment(
        {"sha": "abc123", "author": "x", "role": "dev", "client": "aris4u"}
    )
    assert fixture.resp.status == 400


def test_post_comment_is_retrievable(tmp_path: Path) -> None:
    """A comment inserted via _project_comment can be read back with list_comments."""
    # Add repo root to sys.path so we can import tools.project_timeline.
    repo_root = Path(__file__).parent.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from tools import project_timeline as pt  # type: ignore[import]

    repo = _make_git_repo(tmp_path)
    db = _make_db(tmp_path)
    fixture = _make_handler(repo, db)

    sha = "deadbeef"
    fixture.handler._project_comment(
        {"sha": sha, "author": "qa", "role": "reviewer", "body": "LGTM", "client": "aris4u"}
    )
    assert fixture.resp.status == 200

    comments = pt.list_comments(db_path=db, commit_sha=sha)
    assert len(comments) == 1
    assert comments[0]["body"] == "LGTM"
    assert comments[0]["author"] == "qa"
    assert comments[0]["role"] == "reviewer"


# ---------------------------------------------------------------------------
# Tests: CSRF guard on POST /project/comment (via do_POST routing)
# ---------------------------------------------------------------------------

def test_post_comment_blocked_cross_origin(tmp_path: Path) -> None:
    """POST /project/comment with a cross-site Origin is rejected by do_POST guard."""
    repo = _make_git_repo(tmp_path)
    out_dir = repo / "out"
    out_dir.mkdir(exist_ok=True)
    HandlerClass = server._make_handler(repo, out_dir)

    resp = _FakeResponse()

    # h is Any: dynamically created inner class, bypassing __init__ for testing.
    h: Any = HandlerClass.__new__(HandlerClass)
    h.wfile = resp
    h.server = MagicMock()

    # Simulate a cross-site browser request.
    h.headers = {
        "Origin": "http://evil.example.com",
        "Host": "127.0.0.1:8787",
        "Sec-Fetch-Site": "cross-site",
        "Content-Length": "0",
    }
    h.rfile = io.BytesIO(b"")
    h.path = "/project/comment"

    captured: dict[str, Any] = {}

    def fake_send(code: int, body: bytes, _: str) -> None:
        captured["status"] = code
        captured["body"] = body

    h._send = fake_send

    h.do_POST()
    assert captured.get("status") == 403


# ---------------------------------------------------------------------------
# Tests: manifest completeness (new routes are documented)
# ---------------------------------------------------------------------------

def test_project_routes_in_manifest() -> None:
    """All three /project* routes appear in ENDPOINTS."""
    paths = {e["path"] for e in server.ENDPOINTS}
    assert "/project" in paths
    assert "/project/stream" in paths
    assert "/project/comment" in paths


def test_project_endpoints_well_formed() -> None:
    """Each /project* entry has required fields."""
    for e in server.ENDPOINTS:
        if not e["path"].startswith("/project"):
            continue
        assert e.get("purpose"), f"missing purpose: {e['path']}"
        assert e.get("method") in {"GET", "POST"}, f"invalid method: {e['path']}"
