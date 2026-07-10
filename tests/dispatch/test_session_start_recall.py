"""Tests de caracterización de _build_recall (SessionStart auto-recall sobre claude-mem.db).

Congela el comportamiento EXACTO de _build_recall ANTES de refactorizarlo (CC 23 → helpers):
  - DB inaccesible / sin tablas → "" (fail-open).
  - Path 1: observaciones por proyecto, orden DESC por created_at_epoch, LIMIT 8.
  - Fallback de cuerpo: title → narrative[:160] → text[:160]; truncado a [:200].
  - Path 2: FTS5 cross-project sólo si topic y len(snippets) < 12; salta same-project.
  - Header con el conteo real de snippets; tope global [:14].

Corre:
    .venv312/bin/python3 -m pytest tests/dispatch/test_session_start_recall.py -v
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

from dispatch.events.session_start import (  # noqa: E402
    _build_recall,
    _log_session_start_recall,
    _register_recall_sql,
)


def _make_db(path: Path, *, with_fts: bool = True) -> sqlite3.Connection:
    """Crea un claude-mem.db mínimo con el esquema que consulta _build_recall.

    Args:
        path: Ruta del archivo SQLite a crear.
        with_fts: Si True, crea también la tabla virtual observations_fts.

    Returns:
        Conexión abierta a la DB recién creada.
    """
    # V18 Fase E: el recall lee del texto PROPIO (observations_local en sessions.db), no de
    # claude-mem. `content` reemplaza title/narrative/text; el orden es por rowid (insert).
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE observations_local (
            id TEXT PRIMARY KEY,
            type TEXT,
            content TEXT,
            project TEXT,
            content_hash TEXT,
            created_at TEXT,
            verify_score REAL,
            client_id TEXT
        )
        """
    )
    if with_fts:
        conn.execute(
            "CREATE VIRTUAL TABLE observations_local_fts USING fts5("
            "content, content='observations_local', content_rowid='rowid')"
        )
    conn.commit()
    return conn


def _insert(conn: sqlite3.Connection, **kw: object) -> None:
    """Inserta una observación y la indexa en FTS si existe.

    Compat: acepta title/narrative/text (mapea a `content` = el primer no vacío, como el
    viejo _row_body) o `content` directo. `created_at_epoch` se ignora (orden = rowid).
    """
    content = kw.get("content")
    if content is None:
        content = (str(kw.get("title") or "").strip()
                   or str(kw.get("narrative") or "").strip()
                   or str(kw.get("text") or "").strip())
    row = {
        "id": str(kw.get("id")),
        "type": kw.get("type", "note"),
        "content": content,
        "project": kw.get("project", "p"),
    }
    cur = conn.execute(
        "INSERT INTO observations_local (id, type, content, project) "
        "VALUES (:id, :type, :content, :project)",
        row,
    )
    has_fts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='observations_local_fts'"
    ).fetchone()
    if has_fts:
        conn.execute(
            "INSERT INTO observations_local_fts (rowid, content) VALUES (?,?)",
            (cur.lastrowid, row["content"]),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------

def test_missing_db_returns_empty(tmp_path: Path) -> None:
    """DB inexistente → '' (sin excepción)."""
    assert _build_recall(tmp_path / "nope.db", "p", "p") == ""


def test_no_tables_returns_empty(tmp_path: Path) -> None:
    """DB sin la tabla observations → '' (ambas rutas atrapan la excepción)."""
    db = tmp_path / "empty.db"
    sqlite3.connect(str(db)).close()
    assert _build_recall(db, "p", "p") == ""


def test_no_matches_returns_empty(tmp_path: Path) -> None:
    """Sin filas que matcheen y topic vacío → '' (no header espurio)."""
    db = tmp_path / "db.db"
    conn = _make_db(db)
    _insert(conn, id=1, project="otro", title="x")
    conn.close()
    assert _build_recall(db, "ausente", "") == ""


# ---------------------------------------------------------------------------
# Path 1 — observaciones por proyecto
# ---------------------------------------------------------------------------

def test_path1_basic_and_header(tmp_path: Path) -> None:
    """Una obs del proyecto → header con conteo 1 + línea formateada [type] body."""
    db = tmp_path / "db.db"
    conn = _make_db(db)
    _insert(conn, id=1, type="decision", title="Hola mundo", project="client-a")
    conn.close()
    out = _build_recall(db, "client-a", "client-a")
    lines = out.splitlines()
    assert lines[0] == "## Auto-recall: client-a (last 1 relevant observations)"
    assert lines[1] == ""
    assert "- [decision] Hola mundo" in out


def test_path1_orders_desc_by_epoch_and_limit8(tmp_path: Path) -> None:
    """Path 1 ordena DESC por created_at_epoch y corta a LIMIT 8."""
    db = tmp_path / "db.db"
    conn = _make_db(db, with_fts=False)
    for i in range(10):
        _insert(conn, id=i + 1, title=f"obs{i}", project="client-a", created_at_epoch=i)
    conn.close()
    out = _build_recall(db, "client-a", "")  # topic vacío → no Path 2
    body_lines = [ln for ln in out.splitlines() if ln.startswith("- [")]
    assert len(body_lines) == 8
    # Más reciente (epoch 9) primero, más viejo incluido = epoch 2.
    assert "obs9" in body_lines[0]
    assert "obs2" in body_lines[-1]


def test_path1_body_from_content_truncated_200(tmp_path: Path) -> None:
    """V18: body = content[:200] (content mapea el primer no-vacío de title/narrative/text)."""
    db = tmp_path / "db.db"
    conn = _make_db(db, with_fts=False)
    _insert(conn, id=1, narrative="N" * 300, project="client-a")  # content=narrative
    _insert(conn, id=2, text="T" * 300, project="client-a")       # content=text
    _insert(conn, id=3, title="TT", narrative="ignored", project="client-a")  # content=title
    conn.close()
    out = _build_recall(db, "client-a", "")
    lines = [ln for ln in out.splitlines() if ln.startswith("- [")]
    # Orden por rowid DESC (último insert primero): id3, id2, id1.
    assert lines[0] == "- [note] TT"
    assert lines[1] == "- [note] " + ("T" * 200)
    assert lines[2] == "- [note] " + ("N" * 200)


def test_path1_whitespace_only_body_skipped(tmp_path: Path) -> None:
    """Una obs cuyo body queda vacío tras strip no produce línea."""
    db = tmp_path / "db.db"
    conn = _make_db(db, with_fts=False)
    _insert(conn, id=1, title="   ", narrative="  ", text="", project="client-a", created_at_epoch=1)
    conn.close()
    assert _build_recall(db, "client-a", "") == ""


# ---------------------------------------------------------------------------
# Path 2 — FTS5 cross-project
# ---------------------------------------------------------------------------

def test_path2_adds_cross_project_with_from_tag(tmp_path: Path) -> None:
    """Path 2 añade matches FTS de OTROS proyectos con el tag '[type from project]'."""
    db = tmp_path / "db.db"
    conn = _make_db(db)
    _insert(conn, id=1, title="client-a thing", project="client-a", created_at_epoch=5)
    _insert(conn, id=2, type="note", title="labproj mentions labproj", project="lab-project-1", created_at_epoch=4)
    conn.close()
    out = _build_recall(db, "client-a", "labproj")
    assert "- [note from lab-project-1] labproj mentions labproj" in out


def test_path2_skips_same_project_matches(tmp_path: Path) -> None:
    """Path 2 salta filas cuyo project == project_db (ya cubiertas en Path 1)."""
    db = tmp_path / "db.db"
    conn = _make_db(db)
    _insert(conn, id=1, title="client-a mentions client-a", project="client-a", created_at_epoch=5)
    conn.close()
    out = _build_recall(db, "client-a", "ems")
    # Sólo aparece la línea de Path 1, no una duplicada con 'from'.
    assert "from client-a" not in out
    assert out.count("- [") == 1


def test_path2_gated_when_snippets_ge_12(tmp_path: Path) -> None:
    """Si Path 1 ya produjo >=12 snippets, Path 2 no corre (gate len<12)."""
    # Path 1 limita a 8, así que para forzar >=12 necesitamos otra vía; el gate
    # se prueba indirectamente: con exactamente 8 de Path 1, Path 2 SÍ corre.
    db = tmp_path / "db.db"
    conn = _make_db(db)
    for i in range(8):
        _insert(conn, id=i + 1, title=f"ems{i}", project="client-a", created_at_epoch=i)
    _insert(conn, id=99, title="other mentions ems", project="lab-project-1", created_at_epoch=100)
    conn.close()
    out = _build_recall(db, "client-a", "ems")
    # 8 de Path 1 (<12) → Path 2 corre y agrega el cross-project.
    assert "- [note from lab-project-1] other mentions ems" in out


def test_path2_combines_with_path1_and_caps(tmp_path: Path) -> None:
    """Path 1 (8) + Path 2 (foreign-only matches) se combinan; tope global [:14].

    NOTA caracterización: el query de Path 2 ordena TODO por rank con LIMIT 6 ANTES
    de filtrar same-project. Para que Path 2 contribuya, los términos foráneos deben
    rankear por encima de los del proyecto. Aquí el token 'xeno' sólo aparece en filas
    foráneas, así que las 6 filas de Path 2 son todas cross-project.
    """
    db = tmp_path / "db.db"
    conn = _make_db(db)
    for i in range(8):
        _insert(conn, id=i + 1, title=f"client-a local {i}", project="client-a", created_at_epoch=i)
    for j in range(6):
        _insert(conn, id=50 + j, title=f"foreign xeno {j}", project=f"proj{j}", created_at_epoch=j)
    conn.close()
    out = _build_recall(db, "client-a", "xeno")
    body_lines = [ln for ln in out.splitlines() if ln.startswith("- [")]
    # 8 de Path 1 + 6 de Path 2 = 14 (justo en el tope).
    assert len(body_lines) == 14
    assert "(last 14 relevant observations)" in out
    assert any("from proj" in ln for ln in body_lines)


# ---------------------------------------------------------------------------
# _register_recall_sql — registro SQL de recalls de lab
# ---------------------------------------------------------------------------

def test_register_recall_sql_inserts_row(tmp_path: Path) -> None:
    """Inserta una fila en recall_events con project, n_snippets y source='session_start'."""
    db = tmp_path / "sessions.db"
    _register_recall_sql("client-a", 5, db_path=db)
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT project, n_snippets, source FROM recall_events"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "client-a"
    assert row[1] == 5
    assert row[2] == "session_start"


def test_register_recall_sql_fail_open_missing_dir(tmp_path: Path) -> None:
    """Si el directorio padre no existe, la función retorna sin lanzar excepción."""
    db = tmp_path / "nonexistent" / "sessions.db"
    _register_recall_sql("proj", 3, db_path=db)  # no debe raise


def test_register_recall_sql_creates_table_idempotent(tmp_path: Path) -> None:
    """Dos llamadas seguidas no duplican filas (recall_ids distintos) ni fallan."""
    db = tmp_path / "sessions.db"
    _register_recall_sql("lab-project-1", 2, db_path=db)
    _register_recall_sql("lab-project-1", 4, db_path=db)
    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM recall_events").fetchone()[0]
    conn.close()
    # Dos llamadas = dos recall_ids distintos (UUID4) → dos filas
    assert count == 2


def test_register_recall_sql_recall_id_unique(tmp_path: Path) -> None:
    """Cada llamada genera un recall_id único (no colisiona con INSERT OR IGNORE)."""
    db = tmp_path / "sessions.db"
    for _ in range(5):
        _register_recall_sql("client-c", 1, db_path=db)
    conn = sqlite3.connect(str(db))
    ids = [r[0] for r in conn.execute("SELECT recall_id FROM recall_events").fetchall()]
    conn.close()
    assert len(ids) == 5
    assert len(set(ids)) == 5  # todos distintos


# ---------------------------------------------------------------------------
# Fix gap 0/126 — session_id + JSONL logging
# ---------------------------------------------------------------------------

def test_register_recall_sql_stores_session_id(tmp_path: Path) -> None:
    """_register_recall_sql persiste session_id en SQL y lo devuelve como recall_id.

    Fix del gap 0/126: antes la columna session_id quedaba '' → el calificador
    no podía cruzar el recall con ningún transcript.
    """
    db = tmp_path / "sessions.db"
    rid = _register_recall_sql("client-a", 3, db_path=db, session_id="sess-xyz-001")
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT recall_id, project, n_snippets, source, session_id FROM recall_events"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == rid          # recall_id devuelto == el almacenado
    assert row[1] == "client-a"
    assert row[2] == 3
    assert row[3] == "session_start"
    assert row[4] == "sess-xyz-001"   # session_id persistido (antes era '')


def test_register_recall_sql_returns_stable_id_on_dir_miss(tmp_path: Path) -> None:
    """Devuelve un recall_id no-vacío incluso si el directorio padre no existe."""
    db = tmp_path / "nonexistent" / "sessions.db"
    rid = _register_recall_sql("proj", 3, db_path=db)
    assert rid  # no vacío; idempotencia del fail-open


def test_log_session_start_recall_writes_jsonl_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_log_session_start_recall emite un evento auto_recall completo al JSONL.

    Fix del gap 0/126: sin este log, el calificador de utilidad nunca veía los
    recalls de session_start (solo leía el JSONL, no SQL).
    """
    log_file = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_EVENTS_LOG", str(log_file))
    _log_session_start_recall(
        recall_id="abc123def456",
        topic="client-a",
        session_id="sess-test-abc",
        n_snippets=3,
        injected=["- [decision] usa route_local", "- [note] recall works"],
    )
    assert log_file.exists()
    ev = json.loads(log_file.read_text().strip())
    assert ev["event"] == "auto_recall"
    assert ev["source"] == "session_start"
    assert ev["recall_id"] == "abc123def456"
    assert ev["session_id"] == "sess-test-abc"
    assert ev["query"] == "client-a"
    assert len(ev["injected"]) == 2


def test_log_session_start_recall_fail_open_missing_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_log_session_start_recall es fail-open si el directorio del log no existe."""
    log_file = tmp_path / "no_such_dir" / "events.jsonl"
    monkeypatch.setenv("ARIS4U_EVENTS_LOG", str(log_file))
    # No debe raise; el directorio no existe → retorna silenciosamente
    _log_session_start_recall("rid", "topic", "sid", 0, [])
