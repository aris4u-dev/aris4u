"""Tests del write-path de client_id en recall_events (Move #1, 2026-07-05).

Diagnóstico: el 95.5% de recall_events quedaba con client vacío/NULL.
  - session_start (100% NULL): _register_recall_sql omitía la columna `client`.
  - user_prompt (93.4% NULL): _append_auto_recall solo tomaba ARIS4U_CLIENT (que solo
    se setea para client dirs); for top-level projects (aris4u, lab-project-1…) was empty.

Estos tests fijan los dos fixes:
  1. _register_recall_sql escribe `client = project`.
  2. _append_auto_recall cae a resolve_client_from_path(cwd) y taggea la telemetría.

Corre:
    .venv312/bin/python3 -m pytest tests/dispatch/test_client_tagging_writepath.py -v
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"
for _p in (str(HOOKS), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dispatch.events import user_prompt_submit as ups  # noqa: E402
from dispatch.events.session_start import _register_recall_sql  # noqa: E402

# ---------------------------------------------------------------------------
# Fix 1 — session_start: recall_events.client = project (antes 100% NULL)
# ---------------------------------------------------------------------------


def test_register_recall_sql_tags_client_with_project(tmp_path: Path) -> None:
    """El recall de session_start queda con `client` = project (no vacío)."""
    db = tmp_path / "sessions.db"
    _register_recall_sql("client-b", 5, db_path=db, session_id="s-1")
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT project, client, source FROM recall_events").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "client-b"  # project
    assert row[1] == "client-b"  # client — antes quedaba '' (bug 100% NULL)
    assert row[2] == "session_start"


# ---------------------------------------------------------------------------
# Fix 2 — user_prompt: fallback a resolve_client_from_path taggea la telemetría
# ---------------------------------------------------------------------------


def test_auto_recall_fallback_tags_client_from_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin ARIS4U_CLIENT, el auto-recall resuelve el cliente por cwd (.aris-client)
    y lo emite en la telemetría auto_recall (antes quedaba '')."""
    # cwd tipo proyecto top-level (fuera de 03-clients/) con marcador de cliente.
    proj = tmp_path / "myproj"
    proj.mkdir()
    (proj / ".aris-client").write_text("client-b\n")

    # Sin ARIS4U_CLIENT en el entorno → se fuerza el fallback.
    monkeypatch.delenv("ARIS4U_CLIENT", raising=False)

    # search mockeada: aísla el test de la DB/vector-store viva (rápido y determinista).
    from engine.v16 import session_manager as sm

    monkeypatch.setattr(
        sm,
        "search",
        lambda *a, **k: {"semantic": [], "decisions": [], "guards": []},
    )

    # Telemetría a un JSONL temporal (no contamina el log de producción).
    events_log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_EVENTS_LOG", str(events_log))

    parts: list[str] = []
    ups._append_auto_recall(parts, "migrar el modulo de facturacion", "implementation", str(proj))

    # The auto_recall event must carry client="client-b" resolved by cwd.
    lines = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    recall_events = [e for e in lines if e.get("event") == "auto_recall"]
    assert recall_events, "no se emitió evento auto_recall"
    assert recall_events[-1]["client"] == "client-b"


def test_auto_recall_fallback_fail_open_no_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cwd sin cliente resoluble → client='' (comportamiento previo), NUNCA rompe."""
    proj = tmp_path / "orphan"
    proj.mkdir()
    monkeypatch.delenv("ARIS4U_CLIENT", raising=False)

    from engine.v16 import session_manager as sm

    monkeypatch.setattr(
        sm,
        "search",
        lambda *a, **k: {"semantic": [], "decisions": [], "guards": []},
    )
    events_log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_EVENTS_LOG", str(events_log))

    parts: list[str] = []
    ups._append_auto_recall(parts, "una consulta cualquiera", "implementation", str(proj))

    lines = [json.loads(ln) for ln in events_log.read_text().splitlines() if ln.strip()]
    recall_events = [e for e in lines if e.get("event") == "auto_recall"]
    assert recall_events, "no se emitió evento auto_recall"
    assert recall_events[-1]["client"] == ""


# ---------------------------------------------------------------------------
# Fix ítem D — session_end: _warn_client_id_null es fail-loud, no fail-silencioso
# ---------------------------------------------------------------------------


def test_warn_client_id_null_emits_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """_warn_client_id_null escribe a stderr con el session_id y la location."""
    from dispatch.events.session_end import _warn_client_id_null

    monkeypatch.delenv("ARIS4U_LOG_FILE", raising=False)

    _warn_client_id_null("ses-test-001", "observations_local")

    captured = capsys.readouterr()
    assert "ses-test-001" in captured.err
    assert "observations_local" in captured.err
    assert "client_id=NULL" in captured.err


def test_warn_client_id_null_logs_to_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_warn_client_id_null emite evento client_id_null_writepath al JSONL de telemetría."""
    import json as _json

    from dispatch.events.session_end import _warn_client_id_null

    log_file = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log_file))

    _warn_client_id_null("ses-test-002", "save_digest")

    lines = [_json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    events = [e for e in lines if e.get("event") == "client_id_null_writepath"]
    assert events, "no se emitió evento client_id_null_writepath en el JSONL"
    ev = events[-1]
    assert ev["session_id"] == "ses-test-002"
    assert ev["location"] == "save_digest"


def test_warn_client_id_null_fail_open_no_log_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Sin ARIS4U_LOG_FILE configurado, _warn_client_id_null NO rompe y sigue emitiendo stderr."""
    from dispatch.events.session_end import _warn_client_id_null

    monkeypatch.delenv("ARIS4U_LOG_FILE", raising=False)

    # No debe lanzar excepción bajo ninguna circunstancia.
    _warn_client_id_null("ses-test-003", "observations_local")

    captured = capsys.readouterr()
    # stderr sigue funcionando aunque no haya JSONL.
    assert "client_id=NULL" in captured.err


def test_mirror_to_claude_mem_warns_when_client_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """_mirror_to_claude_mem loguea a stderr cuando detect_client() devuelve None (ítem D)."""
    from dispatch.events import session_end as se
    from engine.v16 import session_manager as sm

    # Forzar detect_client() → None (sin env, cwd fuera de 03-clients/, sin bridge).
    monkeypatch.delenv("ARIS4U_CLIENT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(sm, "detect_client", lambda: None)

    # Redirigir la DB a un path temporal (no contamina sessions.db real).
    db_path = tmp_path / "sessions.db"
    monkeypatch.setattr(sm, "_connect", lambda: __import__("sqlite3").connect(str(db_path)))
    monkeypatch.setattr(sm, "init_db", lambda: None)

    monkeypatch.delenv("ARIS4U_LOG_FILE", raising=False)

    # Debe completar sin excepción (fail-open) y emitir a stderr (fail-loud).
    se._mirror_to_claude_mem("ses-warn-001", "summary text", "dec", "guards", 1, 10)

    captured = capsys.readouterr()
    assert "client_id=NULL" in captured.err
    assert "observations_local" in captured.err


def test_mirror_to_claude_mem_no_warn_when_client_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Cuando detect_client() resuelve un cliente, NO se emite el warning de NULL."""
    import sqlite3 as _sqlite3

    from dispatch.events import session_end as se
    from engine.v16 import session_manager as sm

    monkeypatch.setattr(sm, "detect_client", lambda: "client-b")

    db_path = tmp_path / "ses_ok.db"
    real_conn = _sqlite3.connect(str(db_path))
    real_conn.execute(
        "CREATE TABLE observations_local "
        "(id TEXT, project TEXT, type TEXT, content TEXT, "
        "content_hash TEXT, created_at TEXT, client_id TEXT)"
    )
    real_conn.commit()
    real_conn.close()

    monkeypatch.setattr(sm, "_connect", lambda: _sqlite3.connect(str(db_path)))
    monkeypatch.setattr(sm, "init_db", lambda: None)
    monkeypatch.delenv("ARIS4U_LOG_FILE", raising=False)

    se._mirror_to_claude_mem("ses-ok-001", "summary", "dec", "guards", 0, 0)

    captured = capsys.readouterr()
    assert "client_id=NULL" not in captured.err
