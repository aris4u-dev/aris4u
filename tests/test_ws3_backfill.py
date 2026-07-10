"""Tests para tools/ws3_backfill_vectors.py — write-path de memoria (gap del audit: 0 tests).

Cubre las funciones puras + collectors con DBs temporales y Ollama mockeado.
NUNCA toca las DBs reales ni llama a Ollama.
"""
import json
import sqlite3
from unittest.mock import MagicMock, patch

import tools.ws3_backfill_vectors as ws3
from engine.v16.config import EMBED_DIM


def test_hash_deterministic_and_content_sensitive() -> None:
    assert ws3._hash("hola") == ws3._hash("hola")
    assert ws3._hash("hola") != ws3._hash("hola ")


def test_collect_decisions(tmp_path, monkeypatch) -> None:
    db = tmp_path / "sessions.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY, decision TEXT, "
        "rationale TEXT, domain TEXT, client_id TEXT)"
    )
    con.executemany(
        "INSERT INTO decisions (decision, rationale, domain, client_id) VALUES (?,?,?,?)",
        [
            ("usar bge-m3", "ganó el A/B", "infra", "aris4u"),
            ("decision sola", None, "x", None),
            ("   ", None, None, None),  # blank → excluida por el WHERE trim()
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(ws3, "SESSIONS_DB", db)

    items = ws3.collect_decisions()
    assert len(items) == 2  # la blank se excluye
    assert items[0] == ("decisions", "1", "usar bge-m3 — ganó el A/B", "aris4u", "infra")
    assert items[1][2] == "decision sola"  # rationale None → solo decision
    assert items[1][3] == ""  # client_id None → ""


def test_collect_observations_from_local(tmp_path, monkeypatch) -> None:
    # V18 Fase E: el backfill indexa observations_local (sessions.db), no claude-mem.
    db = tmp_path / "sessions.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE observations_local (id TEXT PRIMARY KEY, content TEXT, "
        "type TEXT, client_id TEXT)"
    )
    con.executemany(
        "INSERT INTO observations_local (id, content, type, client_id) VALUES (?,?,?,?)",
        [
            ("o1", "Titulo — narrativa", "note", "client-c"),
            ("o2", "", "empty", None),  # content vacío → excluida
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(ws3, "SESSIONS_DB", db)

    items = ws3.collect_observations()
    assert len(items) == 1
    assert items[0][0] == "observations"
    assert items[0][2] == "Titulo — narrativa"
    assert items[0][3] == "client-c"


@patch("tools.ws3_backfill_vectors.subprocess.run")
def test_embed_batch_ok(mock_run) -> None:
    vec = [0.1] * EMBED_DIM
    mock_run.return_value = MagicMock(stdout=json.dumps({"embeddings": [vec, vec]}))
    assert ws3.embed_batch(["a", "b"]) == [vec, vec]


@patch("tools.ws3_backfill_vectors.subprocess.run")
def test_embed_batch_fallback_per_item(mock_run) -> None:
    vec = [0.2] * EMBED_DIM

    def side_effect(cmd, **kw):
        body = cmd[4]
        if '"input"' in body:  # llamada batch → dim mala, se rechaza
            return MagicMock(stdout=json.dumps({"embeddings": [[0.0] * 3]}))
        return MagicMock(stdout=json.dumps({"embedding": vec}))  # per-item → ok

    mock_run.side_effect = side_effect
    assert ws3.embed_batch(["a"]) == [vec]


@patch("tools.ws3_backfill_vectors.subprocess.run")
def test_embed_batch_bad_dim_returns_none(mock_run) -> None:
    # batch sin 'embeddings' + per-item con dim incorrecta → None (no se indexa basura)
    mock_run.return_value = MagicMock(stdout=json.dumps({"embedding": [0.0] * 3}))
    assert ws3.embed_batch(["a"]) == [None]
