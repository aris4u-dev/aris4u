"""Tests for Inc.3 cowork additions:

  - GET /project/comments endpoint (handler + routing + CSRF guard + manifest)
  - _render_proyecto() HTML anchors (section id, EventSource hook, comment form)

Style mirrors test_project_timeline_endpoints.py: direct unit-test via fake
HTTP handler, isolated tmp_path DBs, no live sessions.db touched.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

_CONSOLE_ROOT = Path(__file__).parent.parent
if str(_CONSOLE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONSOLE_ROOT))

from aris4u_console import server  # noqa: E402


# ---------------------------------------------------------------------------
# Shared fixtures (mirrors test_project_timeline_endpoints.py)
# ---------------------------------------------------------------------------

def _make_git_repo(tmp_path: Path) -> Path:
    """Minimal git repo with one commit."""
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
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
    return repo


def _make_db(tmp_path: Path) -> Path:
    """Minimal sessions.db with required tables including cowork_comments."""
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
        "CREATE TABLE IF NOT EXISTS cowork_comments "
        "(id INTEGER PRIMARY KEY, commit_sha TEXT NOT NULL, author TEXT, "
        " role TEXT, body TEXT NOT NULL, client_id TEXT, "
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()
    return db


class _FakeResponse:
    def __init__(self) -> None:
        self.status: int = 0
        self.headers: dict[str, str] = {}
        self.body: bytes = b""

    def write(self, _: bytes) -> None:
        pass

    def flush(self) -> None:
        pass


def _make_handler(repo: Path, db: Path) -> Any:
    """Return a patched Handler instance wired to fake socket."""
    out_dir = repo / "out"
    out_dir.mkdir(exist_ok=True)
    HandlerClass = server._make_handler(repo, out_dir)

    resp = _FakeResponse()
    h: Any = HandlerClass.__new__(HandlerClass)
    h.wfile = resp
    h.headers = {}
    h.server = MagicMock()

    def fake_send(code: int, body: bytes, _: str) -> None:
        resp.status = code
        resp.body = body

    h._send = fake_send

    def fake_json(obj: dict) -> None:
        resp.status = 200
        resp.body = json.dumps(obj, ensure_ascii=False).encode()

    h._json = fake_json

    def fake_db() -> Path:
        return db

    h._sessions_db_path = fake_db
    return h, resp


# ---------------------------------------------------------------------------
# Tests: GET /project/comments
# ---------------------------------------------------------------------------

def test_get_comments_requires_client(tmp_path: Path) -> None:
    """GET /project/comments without ?client= must return 400."""
    repo = _make_git_repo(tmp_path)
    db = _make_db(tmp_path)
    h, resp = _make_handler(repo, db)
    h._origin_ok = lambda: True
    h._project_comments("sha=abc123")
    assert resp.status == 400
    data = json.loads(resp.body)
    assert "client" in data["error"].lower() or "sha" in data["error"].lower()


def test_get_comments_requires_sha(tmp_path: Path) -> None:
    """GET /project/comments without ?sha= must return 400."""
    repo = _make_git_repo(tmp_path)
    db = _make_db(tmp_path)
    h, resp = _make_handler(repo, db)
    h._origin_ok = lambda: True
    h._project_comments("client=aris4u")
    assert resp.status == 400


def test_get_comments_empty_when_none(tmp_path: Path) -> None:
    """GET /project/comments with valid params returns empty list when no comments."""
    repo = _make_git_repo(tmp_path)
    db = _make_db(tmp_path)
    h, resp = _make_handler(repo, db)
    h._origin_ok = lambda: True
    h._project_comments("client=aris4u&sha=deadbeef")
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["available"] is True
    assert data["comments"] == []
    assert data["client"] == "aris4u"
    assert data["sha"] == "deadbeef"


def test_get_comments_returns_inserted_comment(tmp_path: Path) -> None:
    """Comments inserted via POST /project/comment are visible via GET /project/comments."""
    repo = _make_git_repo(tmp_path)
    db = _make_db(tmp_path)
    h, resp = _make_handler(repo, db)

    # Insert via POST handler.
    h._project_comment({
        "sha": "abc123",
        "author": "user-a",
        "role": "dev",
        "body": "Looks solid",
        "client": "aris4u",
    })
    assert resp.status == 200

    # Read back via GET handler.
    h._project_comments("client=aris4u&sha=abc123")
    assert resp.status == 200
    data = json.loads(resp.body)
    assert data["available"] is True
    assert len(data["comments"]) == 1
    cm = data["comments"][0]
    assert cm["body"] == "Looks solid"
    assert cm["author"] == "user-a"


def test_get_comments_client_isolation(tmp_path: Path) -> None:
    """GET /project/comments only returns comments for the requested client."""
    repo = _make_git_repo(tmp_path)
    db = _make_db(tmp_path)
    h, resp = _make_handler(repo, db)

    # Insert comment for client_a.
    h._project_comment({
        "sha": "sha001",
        "author": "a",
        "role": "dev",
        "body": "For client_a",
        "client": "client_a",
    })
    # Insert comment for client_b on the same SHA.
    h._project_comment({
        "sha": "sha001",
        "author": "b",
        "role": "dev",
        "body": "For client_b",
        "client": "client_b",
    })

    # client_a should only see its own comment.
    h._project_comments("client=client_a&sha=sha001")
    data = json.loads(resp.body)
    assert len(data["comments"]) == 1
    assert data["comments"][0]["body"] == "For client_a"

    # client_b should only see its own comment.
    h._project_comments("client=client_b&sha=sha001")
    data = json.loads(resp.body)
    assert len(data["comments"]) == 1
    assert data["comments"][0]["body"] == "For client_b"


# ---------------------------------------------------------------------------
# Tests: _SENSITIVE_GETS includes /project/comments
# ---------------------------------------------------------------------------

def test_project_comments_in_sensitive_gets() -> None:
    """/project/comments must be in _SENSITIVE_GETS (cross-origin guard)."""
    assert "/project/comments" in server._SENSITIVE_GETS


# ---------------------------------------------------------------------------
# Tests: CSRF guard on GET /project/comments via do_GET routing
# ---------------------------------------------------------------------------

def test_get_comments_blocked_cross_origin(tmp_path: Path) -> None:
    """GET /project/comments with a cross-site Origin is rejected by do_GET guard."""
    repo = _make_git_repo(tmp_path)
    out_dir = repo / "out"
    out_dir.mkdir(exist_ok=True)
    HandlerClass = server._make_handler(repo, out_dir)

    resp = _FakeResponse()
    h: Any = HandlerClass.__new__(HandlerClass)
    h.wfile = resp
    h.server = MagicMock()
    h.headers = {
        "Origin": "http://evil.example.com",
        "Host": "127.0.0.1:8787",
        "Sec-Fetch-Site": "cross-site",
    }
    h.path = "/project/comments?client=aris4u&sha=abc"

    captured: dict[str, Any] = {}

    def fake_send(code: int, body: bytes, ct: str) -> None:
        del body, ct  # firma debe calzar con _send(code, body, ctype); no se usan aquí
        captured["status"] = code

    h._send = fake_send
    h.do_GET()
    assert captured.get("status") == 403


# ---------------------------------------------------------------------------
# Tests: manifest completeness
# ---------------------------------------------------------------------------

def test_project_comments_in_manifest() -> None:
    """/project/comments must appear in ENDPOINTS."""
    paths = {e["path"] for e in server.ENDPOINTS}
    assert "/project/comments" in paths


def test_project_comments_endpoint_well_formed() -> None:
    """The /project/comments ENDPOINTS entry has required fields."""
    for e in server.ENDPOINTS:
        if e["path"] != "/project/comments":
            continue
        assert e.get("method") == "GET"
        assert e.get("purpose")
        break
    else:
        raise AssertionError("/project/comments not found in ENDPOINTS")


# ---------------------------------------------------------------------------
# Tests: _render_proyecto HTML anchors
# ---------------------------------------------------------------------------

def test_render_proyecto_has_section_id() -> None:
    """_render_proyecto() output must contain id=\"proyecto\"."""
    from aris4u_console.render_console import _render_proyecto
    html = _render_proyecto()
    assert 'id="proyecto"' in html


def test_render_proyecto_has_timeline_container() -> None:
    """_render_proyecto() must contain the #proj-timeline anchor."""
    from aris4u_console.render_console import _render_proyecto
    html = _render_proyecto()
    assert "proj-timeline" in html


def test_render_proyecto_has_client_input() -> None:
    """_render_proyecto() must contain the #proj-client input for client selection."""
    from aris4u_console.render_console import _render_proyecto
    html = _render_proyecto()
    assert "proj-client" in html


def test_render_console_html_includes_proyecto_section() -> None:
    """render_console_html() includes the proyecto section in the rendered page."""
    from aris4u_console import render_console
    # Use minimal live/curated dicts to avoid file I/O.
    live: dict = {"git": {"branch": "main", "head": "abc123"}, "generated_at": "2026-01-01T00:00:00",
                  "totals": {"components": 0}}
    curated: dict = {"identity": {}, "inventory": {"groups": []}, "behavior": {}}
    html = render_console.render_console_html(live, curated)
    assert 'id="proyecto"' in html
    assert "proj-timeline" in html
    assert "/project/stream" in html  # EventSource URL referenced in JS


def test_render_console_html_has_proyecto_nav_button() -> None:
    """render_console_html() includes a nav button for the proyecto section."""
    from aris4u_console import render_console
    live: dict = {"git": {"branch": "main", "head": "abc123"}, "generated_at": "2026-01-01T00:00:00",
                  "totals": {"components": 0}}
    curated: dict = {"identity": {}, "inventory": {"groups": []}, "behavior": {}}
    html = render_console.render_console_html(live, curated)
    # Nav button carries data-s="proyecto"
    assert 'data-s="proyecto"' in html
