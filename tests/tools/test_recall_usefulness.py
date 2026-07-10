"""Tests del calificador automático de utilidad de recalls (tools/recall_usefulness.py).

Cubre: tokenización/distintividad, la heurística ``judge`` (contribución marginal usada),
el esquema extendido de recall_feedback (idempotente, respeta marcas manuales), la
extracción de respuesta desde un transcript sintético y el end-to-end de ``evaluate``.

``tools/`` no es paquete → se añade a sys.path como hacen los tests hermanos.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import recall_usefulness as ru  # noqa: E402


# --- tokenización / distintividad -----------------------------------------------------

def test_tokenize_drops_stopwords_and_short() -> None:
    """Quita stopwords ES/EN y tokens cortos; conserva términos de contenido."""
    toks = ru.tokenize("El ruteo de los modelos con route_local")
    assert "ruteo" in toks and "modelos" in toks and "route_local" in toks
    assert "el" not in toks and "los" not in toks and "de" not in toks


def test_tokenize_strips_injected_scaffold() -> None:
    """Con strip_scaffold elimina score/[source#id]/(domain) y viñetas."""
    toks = ru.tokenize(
        "  ~0.65 [decisions#12] usa route_local en model_router.py", strip_scaffold=True
    )
    assert "route_local" in toks and "model_router.py" in toks
    assert "0.65" not in toks and "decisions" not in toks  # andamiaje fuera


def test_is_identifier() -> None:
    """Identificadores = con _ . / - o dígito y longitud >= 4."""
    assert ru.is_identifier("route_local")
    assert ru.is_identifier("model_router.py")
    assert ru.is_identifier("v16.9")
    assert not ru.is_identifier("modelos")
    assert not ru.is_identifier("abc")


# --- judge: la heurística -------------------------------------------------------------

def test_judge_useful_on_identifier_reuse() -> None:
    """Útil: el recall aportó identificadores ausentes del prompt y usados en la acción."""
    injected = ["  ~0.65 [decisions#12] usa route_local en model_router.py para el ruteo"]
    query = "como ruteo los modelos locales"
    response = "edité model_router.py y llamé a route_local con el cliente"
    useful, score, matched = ru.judge(injected, query, response)
    assert useful is True
    assert score >= 2.0
    assert "route_local" in matched and "model_router.py" in matched


def test_judge_useful_on_two_distinctive_terms() -> None:
    """Útil: >=2 términos distintivos nuevos (sin ser identificadores) usados."""
    injected = ["  · (memoria) el sidecar usa embeddings semanticos con hibrido"]
    query = "como busco cosas"
    response = "armé la busqueda con embeddings semanticos sobre el sidecar"
    useful, _score, matched = ru.judge(injected, query, response)
    assert useful is True
    assert {"embeddings", "semanticos", "sidecar"} & set(matched)


def test_judge_not_useful_when_no_marginal_contribution() -> None:
    """No útil: lo único compartido con la respuesta ya estaba en el prompt."""
    injected = ["  ~0.5 [x] ruteo de modelos importante segun la nota"]
    query = "ruteo de modelos"
    response = "hago el ruteo de modelos como siempre"
    useful, score, _matched = ru.judge(injected, query, response)
    assert useful is False
    assert score == 0.0


def test_judge_not_useful_when_unused() -> None:
    """No útil: el recall trae algo nuevo pero la respuesta no lo toca."""
    injected = ["  ~0.5 [x] la capital de francia es paris segun geografia"]
    query = "dame una mano con esto"
    response = "claro, empecemos por revisar el archivo de configuracion"
    useful, _score, _matched = ru.judge(injected, query, response)
    assert useful is False


# --- esquema + persistencia -----------------------------------------------------------

def _conn() -> sqlite3.Connection:
    """sessions.db en memoria con la tabla base de 3 columnas (estado legacy)."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recall_feedback ("
        "recall_id TEXT PRIMARY KEY, useful INTEGER NOT NULL, marked_at TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def test_ensure_schema_adds_columns_idempotent() -> None:
    """ensure_schema añade method/score/detail y es idempotente."""
    conn = _conn()
    ru.ensure_schema(conn)
    ru.ensure_schema(conn)  # segunda vez no debe fallar
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recall_feedback)").fetchall()}
    assert {"recall_id", "useful", "marked_at", "method", "score", "detail"} <= cols


def test_upsert_implicit_writes_and_is_idempotent() -> None:
    """Escribe una marca implícita y la re-evalúa sobre sí misma."""
    conn = _conn()
    ru.ensure_schema(conn)
    assert ru.upsert_implicit(conn, "r1", True, 3.0, "[]") is True
    row = conn.execute(
        "SELECT useful, method FROM recall_feedback WHERE recall_id='r1'"
    ).fetchone()
    assert row == (1, "implicit")
    # re-evaluación: cambia el veredicto a no-útil
    assert ru.upsert_implicit(conn, "r1", False, 0.0, "[]") is True
    assert conn.execute(
        "SELECT useful FROM recall_feedback WHERE recall_id='r1'"
    ).fetchone()[0] == 0


def test_upsert_implicit_never_overwrites_manual() -> None:
    """Una marca method='manual' (juicio humano) NO se pisa."""
    conn = _conn()
    ru.ensure_schema(conn)
    conn.execute(
        "INSERT INTO recall_feedback (recall_id, useful, marked_at, method) "
        "VALUES ('r2', 1, '2026-06-19T00:00:00+00:00', 'manual')"
    )
    conn.commit()
    assert ru.upsert_implicit(conn, "r2", False, 0.0, "[]") is False
    row = conn.execute(
        "SELECT useful, method FROM recall_feedback WHERE recall_id='r2'"
    ).fetchone()
    assert row == (1, "manual")  # intacto


# --- transcript + evaluate ------------------------------------------------------------

def _write_transcript(path: Path, session_id: str) -> None:
    """Escribe un transcript sintético: prompt → respuesta (texto+tool_use) → otro prompt."""
    rows = [
        {"type": "user", "sessionId": session_id,
         "message": {"role": "user", "content": "arregla el medidor de recall del freeze"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "voy a usar route_local en model_router.py"},
            {"type": "tool_use", "name": "Edit",
             "input": {"file_path": "engine/v16/model_router.py"}},
        ]}},
        {"type": "user", "toolUseResult": {"ok": True},
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "listo"}]}},
        {"type": "user",
         "message": {"role": "user", "content": "ahora otra cosa totalmente distinta"}},
        {"type": "assistant", "message": {"role": "assistant",
         "content": [{"type": "text", "text": "esto no debe contarse para el recall"}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))


def test_extract_response_pairs_prompt_with_following_assistant(tmp_path: Path) -> None:
    """extract_response toma la respuesta del asistente y para en el siguiente prompt."""
    tr = tmp_path / "sess.jsonl"
    _write_transcript(tr, "sessA")
    resp = ru.extract_response(tr, "arregla el medidor de recall")
    assert resp is not None
    assert "route_local" in resp and "model_router.py" in resp
    assert "totalmente distinta" not in resp  # no cruza al siguiente turno


def test_find_transcript_globs_by_session(tmp_path: Path) -> None:
    """find_transcript localiza <session_id>.jsonl bajo cualquier subproyecto."""
    proj = tmp_path / "-Users-x"
    proj.mkdir()
    (proj / "sessZ.jsonl").write_text("{}")
    assert ru.find_transcript("sessZ", tmp_path) == proj / "sessZ.jsonl"
    assert ru.find_transcript("nope", tmp_path) is None


def test_evaluate_end_to_end_and_skip_counts(tmp_path: Path) -> None:
    """evaluate juzga el instrumentado y clasifica los descartes correctamente."""
    proj = tmp_path / "-Users-x"
    proj.mkdir()
    _write_transcript(proj / "sessA.jsonl", "sessA")
    injected = ["  ~0.7 [decisions#1] usa route_local en model_router.py para ruteo"]
    events = [
        # instrumentado + transcript + prompt emparejable → se juzga (útil)
        {"recall_id": "ok1", "session_id": "sessA", "injected": injected,
         "query": "arregla el medidor de recall del freeze", "client": ""},
        # sin session_id/injected → no_session
        {"recall_id": "no1", "session_id": "", "injected": [], "query": "x", "client": ""},
        # session_id sin transcript → no_transcript
        {"recall_id": "no2", "session_id": "ghost", "injected": injected,
         "query": "algo", "client": ""},
        # transcript pero query no empareja → no_response
        {"recall_id": "no3", "session_id": "sessA", "injected": injected,
         "query": "prompt que no existe en el transcript", "client": ""},
    ]
    results, skips = ru.evaluate(events, tmp_path)
    assert len(results) == 1 and results[0]["recall_id"] == "ok1"
    assert results[0]["useful"] is True
    assert skips == {"no_session": 1, "no_transcript": 1, "no_response": 1}


# --- SQL: recall_events + stats -------------------------------------------------------

def _conn_with_recall_events() -> sqlite3.Connection:
    """sessions.db en memoria con recall_events y recall_feedback vacíos."""
    conn = sqlite3.connect(":memory:")
    ru.ensure_recall_events_schema(conn)
    ru.ensure_schema(conn)
    return conn


def test_ensure_recall_events_schema_idempotent() -> None:
    """ensure_recall_events_schema crea la tabla y es idempotente en segunda llamada."""
    conn = sqlite3.connect(":memory:")
    ru.ensure_recall_events_schema(conn)
    ru.ensure_recall_events_schema(conn)  # segunda vez no debe fallar
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recall_events)").fetchall()}
    assert {"recall_id", "ts", "project", "n_snippets", "source"} <= cols


def test_sync_jsonl_to_sql_inserts_events(tmp_path: Path) -> None:
    """sync_jsonl_to_sql importa eventos auto_recall del JSONL a recall_events."""
    log = tmp_path / "events.jsonl"
    now = datetime.now(UTC)
    # Evento con session_id e injected (formato moderno)
    log.write_text(
        json.dumps({
            "ts": now.isoformat(),
            "event": "auto_recall",
            "recall_id": "aabbccdd0011",
            "results": 3,
            "query": "test query",
            "client": "client-b",
            "session_id": "sess-abc",
            "injected": ["snippet1", "snippet2", "snippet3"],
        }) + "\n" +
        # Evento no-recall que debe ignorarse
        json.dumps({"ts": now.isoformat(), "event": "depth_inject"}) + "\n"
    )
    conn = sqlite3.connect(":memory:")
    ru.ensure_recall_events_schema(conn)
    since = now - timedelta(hours=1)
    inserted = ru.sync_jsonl_to_sql(conn, log, since)
    assert inserted == 1
    row = conn.execute(
        "SELECT recall_id, source, n_snippets, client FROM recall_events"
    ).fetchone()
    assert row[0] == "aabbccdd0011"
    assert row[1] == "user_prompt"
    assert row[2] == 3
    assert row[3] == "client-b"


def test_sync_jsonl_to_sql_idempotent(tmp_path: Path) -> None:
    """Sincronizar el mismo JSONL dos veces no duplica filas (INSERT OR IGNORE)."""
    log = tmp_path / "events.jsonl"
    ev = {
        "ts": datetime.now(UTC).isoformat(),
        "event": "auto_recall",
        "recall_id": "dedup1234",
        "results": 2,
        "query": "q",
        "client": "",
        "session_id": "s1",
        "injected": ["a", "b"],
    }
    log.write_text(json.dumps(ev) + "\n")
    conn = sqlite3.connect(":memory:")
    ru.ensure_recall_events_schema(conn)
    ru.sync_jsonl_to_sql(conn, log, None)
    ru.sync_jsonl_to_sql(conn, log, None)  # segunda vez
    count = conn.execute("SELECT COUNT(*) FROM recall_events").fetchone()[0]
    assert count == 1


def test_stats_from_sql_computes_weekly_metrics() -> None:
    """stats_from_sql calcula total/útiles/pct_useful por semana."""
    conn = _conn_with_recall_events()
    now = datetime.now(UTC)
    # Insertar 3 recalls: 2 marcados útiles, 1 no útil
    for i, (rid, useful) in enumerate([("r1", 1), ("r2", 1), ("r3", 0)]):
        ts = (now - timedelta(hours=i)).isoformat()
        conn.execute(
            "INSERT INTO recall_events (recall_id, ts, project, n_snippets, source) "
            "VALUES (?, ?, 'p', 2, 'user_prompt')",
            (rid, ts),
        )
        conn.execute(
            "INSERT INTO recall_feedback (recall_id, useful, marked_at, method) "
            "VALUES (?, ?, ?, 'implicit')",
            (rid, useful, ts),
        )
    conn.commit()
    stats = ru.stats_from_sql(conn, days=7)
    assert len(stats) >= 1
    row = stats[0]
    assert row["total"] == 3
    assert row["useful"] == 2
    assert row["pct_useful"] == pytest.approx(66.7, abs=0.1)
    assert row["source"] == "user_prompt"


def test_stats_from_sql_empty_table() -> None:
    """Sin datos en recall_events, stats devuelve lista vacía (sin excepción)."""
    conn = _conn_with_recall_events()
    stats = ru.stats_from_sql(conn, days=7)
    assert stats == []


def test_stats_from_sql_missing_table() -> None:
    """Con DB vacía (sin recall_events), stats retorna [] sin excepción (fail-open)."""
    conn = sqlite3.connect(":memory:")
    stats = ru.stats_from_sql(conn, days=7)
    assert stats == []


# --- Fix gap 0/126 — session_start evaluation ------------------------------------

def _write_session_only_transcript(path: Path) -> None:
    """Transcript sin prompts de usuario: solo turnos de asistente (sesión de lab)."""
    rows = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "uso route_local en model_router.py para el ruteo"},
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "llamo session_manager.search para el recall"},
        ]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows))


def test_full_session_text_collects_all_assistant_turns(tmp_path: Path) -> None:
    """_full_session_text concatena TODOS los turnos del asistente en el transcript."""
    tr = tmp_path / "sessB.jsonl"
    _write_session_only_transcript(tr)
    text = ru._full_session_text(tr)
    assert "route_local" in text
    assert "session_manager.search" in text


def test_full_session_text_empty_on_missing_file(tmp_path: Path) -> None:
    """_full_session_text retorna '' si el transcript no existe (fail-open)."""
    text = ru._full_session_text(tmp_path / "ghost.jsonl")
    assert text == ""


def test_evaluate_session_start_uses_full_session_text(tmp_path: Path) -> None:
    """evaluate usa _full_session_text para events con source='session_start'.

    Fix del gap 0/126: sin esto, los recalls de session_start caían en no_response
    porque extract_response no encontraba su query (el topic, no un prompt real).
    """
    proj = tmp_path / "-Users-x"
    proj.mkdir()
    _write_session_only_transcript(proj / "sessB.jsonl")
    injected = ["- [decision] usa route_local en model_router.py para ruteo"]
    events = [
        {
            "recall_id": "ss1",
            "session_id": "sessB",
            "injected": injected,
            "query": "client-a",        # topic, no un prompt real
            "client": "",
            "source": "session_start",
        },
    ]
    results, skips = ru.evaluate(events, tmp_path)
    assert len(results) == 1
    assert results[0]["recall_id"] == "ss1"
    assert results[0]["useful"] is True
    assert skips == {"no_session": 0, "no_transcript": 0, "no_response": 0}


def test_evaluate_session_start_no_response_counted_correctly(tmp_path: Path) -> None:
    """Si el transcript de session_start no tiene ningún turno asistente → no_response."""
    proj = tmp_path / "-Users-x"
    proj.mkdir()
    # Transcript solo con un prompt de usuario, sin respuestas del asistente
    (proj / "sessC.jsonl").write_text(
        json.dumps({
            "type": "user",
            "message": {"role": "user", "content": "hola"},
        }) + "\n"
    )
    injected = ["- [note] algo"]
    events = [
        {
            "recall_id": "ss2",
            "session_id": "sessC",
            "injected": injected,
            "query": "client-a",
            "client": "",
            "source": "session_start",
        },
    ]
    results, skips = ru.evaluate(events, tmp_path)
    assert len(results) == 0
    assert skips["no_response"] == 1


def test_sync_jsonl_to_sql_preserves_session_start_source(tmp_path: Path) -> None:
    """sync_jsonl_to_sql preserva source='session_start' del evento (no hardcodea 'user_prompt').

    Fix del gap 0/126: antes todos los eventos del JSONL se importaban con
    source='user_prompt', ocultando los recalls de session_start en las métricas.
    """
    log = tmp_path / "events.jsonl"
    now = datetime.now(UTC)
    log.write_text(
        json.dumps({
            "ts": now.isoformat(),
            "event": "auto_recall",
            "recall_id": "ss-event-001",
            "results": 3,
            "query": "client-a",
            "client": "",
            "session_id": "sess-ss-1",
            "source": "session_start",
            "injected": ["- [note] thing"],
        }) + "\n"
    )
    conn = sqlite3.connect(":memory:")
    ru.ensure_recall_events_schema(conn)
    inserted = ru.sync_jsonl_to_sql(conn, log, None)
    assert inserted == 1
    row = conn.execute("SELECT source FROM recall_events").fetchone()
    assert row[0] == "session_start"   # preservado, no 'user_prompt'


def test_sync_jsonl_defaults_to_user_prompt_when_source_absent(tmp_path: Path) -> None:
    """Eventos sin campo 'source' en el JSONL se importan como 'user_prompt' (compat.)."""
    log = tmp_path / "events.jsonl"
    now = datetime.now(UTC)
    log.write_text(
        json.dumps({
            "ts": now.isoformat(),
            "event": "auto_recall",
            "recall_id": "no-source-001",
            "results": 2,
            "query": "algo",
            "client": "",
            "session_id": "s1",
            "injected": [],
        }) + "\n"
    )
    conn = sqlite3.connect(":memory:")
    ru.ensure_recall_events_schema(conn)
    ru.sync_jsonl_to_sql(conn, log, None)
    row = conn.execute("SELECT source FROM recall_events").fetchone()
    assert row[0] == "user_prompt"


# --- Reporte semanal ---------------------------------------------------------------

def _conn_with_two_weeks_data() -> sqlite3.Connection:
    """DB en memoria con 5 recalls útiles esta semana y 2 la anterior."""
    conn = sqlite3.connect(":memory:")
    ru.ensure_recall_events_schema(conn)
    ru.ensure_schema(conn)
    now = datetime.now(UTC)
    # Esta semana: 5 recalls, 3 útiles
    for i, useful in enumerate([1, 1, 1, 0, 0]):
        rid = f"this{i}"
        ts = (now - timedelta(hours=i + 1)).isoformat()
        conn.execute(
            "INSERT INTO recall_events (recall_id, ts, project, n_snippets, source) "
            "VALUES (?, ?, 'p', 2, 'user_prompt')",
            (rid, ts),
        )
        conn.execute(
            "INSERT INTO recall_feedback (recall_id, useful, marked_at, method) "
            "VALUES (?, ?, ?, 'implicit')",
            (rid, useful, ts),
        )
    # Semana anterior: 3 recalls, 1 útil
    for i, useful in enumerate([1, 0, 0]):
        rid = f"prev{i}"
        ts = (now - timedelta(days=8 + i)).isoformat()
        conn.execute(
            "INSERT INTO recall_events (recall_id, ts, project, n_snippets, source) "
            "VALUES (?, ?, 'p', 2, 'user_prompt')",
            (rid, ts),
        )
        conn.execute(
            "INSERT INTO recall_feedback (recall_id, useful, marked_at, method) "
            "VALUES (?, ?, ?, 'implicit')",
            (rid, useful, ts),
        )
    conn.commit()
    return conn


def test_weekly_stats_returns_this_and_last_week() -> None:
    """_weekly_stats devuelve métricas separadas para esta semana y la anterior."""
    conn = _conn_with_two_weeks_data()
    this, last = ru._weekly_stats(conn)
    assert "user_prompt" in this
    assert this["user_prompt"]["total"] == 5
    assert this["user_prompt"]["useful"] == 3
    assert "user_prompt" in last
    assert last["user_prompt"]["total"] == 3
    assert last["user_prompt"]["useful"] == 1
    conn.close()


def test_format_weekly_report_gate_not_met() -> None:
    """_format_weekly_report marca gate NO MET cuando alguna semana tiene <3 útiles."""
    this = {"user_prompt": {"total": 5, "useful": 3, "pct": 60.0}}
    last = {"user_prompt": {"total": 3, "useful": 1, "pct": 33.3}}
    report = ru._format_weekly_report(this, last)
    assert "NO MET" in report
    assert "sem anterior 1/3" in report


def test_format_weekly_report_gate_met() -> None:
    """_format_weekly_report marca gate MET cuando ambas semanas tienen >=3 útiles."""
    this = {"user_prompt": {"total": 10, "useful": 4, "pct": 40.0}}
    last = {"user_prompt": {"total": 8, "useful": 3, "pct": 37.5}}
    report = ru._format_weekly_report(this, last)
    assert "GATE TRAMO 4: MET" in report


def test_append_to_weekly_log_creates_and_appends(tmp_path: Path) -> None:
    """_append_to_weekly_log crea el archivo y hace append en llamadas sucesivas."""
    root = tmp_path
    (root / "logs").mkdir()
    ru._append_to_weekly_log("primer reporte", root)
    ru._append_to_weekly_log("segundo reporte", root)
    content = (root / "logs" / "recall-weekly-report.log").read_text()
    assert "primer reporte" in content
    assert "segundo reporte" in content
