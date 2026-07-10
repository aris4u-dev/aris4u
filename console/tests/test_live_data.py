"""Tests de los lectores VIVOS (live_data) — memoria, telemetría y hooks, todo aislado en tmp."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aris4u_console import live_data as L  # noqa: E402


def _make_db(repo: Path) -> None:
    """Crea una sessions.db mínima con las columnas que leen los lectores."""
    (repo / "data").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(repo / "data" / "sessions.db")
    conn.executescript("""
        CREATE TABLE decisions(decision TEXT, domain TEXT, locked INTEGER DEFAULT 0,
            created_at TEXT, client_id TEXT, mem_type TEXT);
        CREATE TABLE guards(pattern TEXT, prevention TEXT, severity TEXT,
            created_at TEXT, client_id TEXT);
        CREATE TABLE digests(date TEXT, summary TEXT, created_at TEXT, client_id TEXT);
        CREATE TABLE recall_feedback(recall_id TEXT PRIMARY KEY, useful INTEGER NOT NULL,
            marked_at TEXT);
    """)
    conn.execute("INSERT INTO decisions VALUES ('decidir A','arch',1,'2026-06-01','aris4u',NULL)")
    conn.execute("INSERT INTO decisions VALUES ('decidir B','db',0,'2026-06-02',NULL,NULL)")
    # provenance (git-commit) y facts (átomos) NO cuentan como decisión de la bitácora:
    conn.execute("INSERT INTO decisions VALUES ('[commit abc] x','git-commit',0,'2026-06-03','aris4u','provenance')")
    conn.execute("INSERT INTO decisions VALUES ('átomo X','combinatorial-optimization',1,'2026-06-03','aris4u','fact')")
    conn.execute("INSERT INTO guards VALUES ('patrón X','evitar Y','high','2026-06-01','client-c')")
    conn.execute("INSERT INTO digests VALUES ('2026-06-02','resumen del día','2026-06-02','aris4u')")
    conn.execute("INSERT INTO recall_feedback VALUES ('r1',1,'2026-06-02')")
    conn.execute("INSERT INTO recall_feedback VALUES ('r2',0,'2026-06-02')")
    conn.commit()
    conn.close()


def _make_log(repo: Path) -> None:
    """Crea un log de eventos jsonl (con una línea corrupta para probar fail-soft)."""
    (repo / "logs").mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"ts": "2026-06-02T10:00:00", "event": "auto_recall",
                    "hook": "auto_recall", "query": "qué decidimos", "n_semantic": 3}),
        json.dumps({"ts": "2026-06-02T10:00:01", "event": "model_hint",
                    "hook": "model_hint", "intent": "code", "model": "opus"}),
        "{ línea corrupta }",
        json.dumps({"ts": "2026-06-02T10:00:02", "event": "mcp_tool",
                    "hook": "mcp_server", "tool": "aris_search"}),
    ]
    (repo / "logs" / "v16.1-events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- memoria --------------------------------------------------------------------------

def test_read_memory_totals_and_clients(tmp_path: Path) -> None:
    """read_memory cuenta totales, agrupa por cliente y trae recientes."""
    _make_db(tmp_path)
    mem = L.read_memory(tmp_path)
    assert mem["available"] is True
    assert mem["totals"]["decisions"] == 2
    assert mem["totals"]["guards"] == 1
    assert mem["totals"]["vectors"] == 0  # no hay aris_vectors.db en tmp
    clients = {c["client"]: c for c in mem["by_client"]}
    assert clients["aris4u"]["decisions"] == 1
    assert clients["(none)"]["decisions"] == 1
    assert mem["recall"]["feedback_total"] == 2
    assert mem["recall"]["useful_rate"] == 0.5


def test_read_memory_unavailable(tmp_path: Path) -> None:
    """Sin sessions.db, read_memory degrada con available=False (no crashea)."""
    mem = L.read_memory(tmp_path)
    assert mem["available"] is False


# --- telemetría -----------------------------------------------------------------------

def test_read_telemetry_parses_and_aggregates(tmp_path: Path) -> None:
    """read_telemetry parsea jsonl (saltando corruptas) y agrega por tipo."""
    _make_log(tmp_path)
    tel = L.read_telemetry(tmp_path, limit=10, window=100)
    assert tel["available"] is True
    assert tel["window"] == 3  # 3 válidas, 1 corrupta descartada
    assert tel["by_type"]["auto_recall"] == 1
    assert tel["recent"][0]["type"] == "mcp_tool"  # más nuevo primero
    # NO debe exponer los eventos crudos (query verbatim + memoria inyectada = fuga de datos)
    assert "events" not in tel


def test_recall_stats_null_query_no_crash(tmp_path: Path) -> None:
    """Un auto_recall con query=None NO debe crashear /memory (campo presente pero None)."""
    _make_db(tmp_path)
    mem = L.read_memory(tmp_path, events=[{"event": "auto_recall", "query": None, "ts": "t1"}])
    assert mem["available"] is True
    assert mem["recall"]["last_recall_query"] == ""


def test_event_summary_salient_and_fallback() -> None:
    """event_summary usa campos salientes por tipo, o cae a las primeras claves."""
    s = L.event_summary({"event": "model_hint", "intent": "code", "model": "opus",
                         "ts": "x", "ruido": "no"})
    assert "intent=code" in s and "model=opus" in s and "ruido" not in s
    fb = L.event_summary({"event": "raro", "a": 1, "b": 2})
    assert "a=1" in fb


def test_tail_lines(tmp_path: Path) -> None:
    """tail_lines devuelve las últimas N líneas no vacías."""
    f = tmp_path / "x.log"
    f.write_text("\n".join(str(i) for i in range(50)) + "\n", encoding="utf-8")
    assert L.tail_lines(f, 3) == ["47", "48", "49"]
    assert L.tail_lines(tmp_path / "nope.log", 5) == []


# --- hooks ----------------------------------------------------------------------------

def test_read_amplifier_roi_and_pending(tmp_path: Path) -> None:
    """read_amplifier computa ROI (disponibilidad/latencia/etiquetas) y lista pendientes."""
    (tmp_path / "logs").mkdir()
    lines = [
        # 2 structure disponibles (una etiquetada útil), 1 critique fría, 1 sin call_id
        json.dumps({"event": "mcp_tool", "tool": "aris_structure", "call_id": "c1",
                    "available": True, "latency_ms": 20000, "ts": "2026-06-20T10:00:00+00:00",
                    "backend": "mlx", "chars": 400}),
        json.dumps({"event": "mcp_tool", "tool": "aris_structure", "call_id": "c2",
                    "available": True, "latency_ms": 40000, "ts": "2026-06-20T11:00:00+00:00"}),
        json.dumps({"event": "mcp_tool", "tool": "aris_critique", "available": False,
                    "ts": "2026-06-20T12:00:00+00:00"}),
        json.dumps({"event": "f1_feedback", "call_id": "c1", "useful": True}),
    ]
    (tmp_path / "logs" / "v16.1-events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    amp = L.read_amplifier(tmp_path)
    assert amp["available"] is True
    assert amp["calls"] == 3 and amp["availability_rate"] == round(2 / 3, 2)
    assert amp["labeled"] == 1 and amp["useful"] == 1
    assert amp["latency_p50"] in (30000, 20000, 40000)  # interpola entre las 2 disponibles
    # c2 disponible y sin etiqueta → pendiente; c1 etiquetada y la fría sin call_id no
    assert [p["call_id"] for p in amp["pending"]] == ["c2"]


def test_append_label_writes_feedback_event(tmp_path: Path) -> None:
    """append_label anexa un evento f1_feedback con la forma canónica del motor."""
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "v16.1-events.jsonl").write_text("", encoding="utf-8")
    res = L.append_label(tmp_path, "abc123", useful=False, note="vago")
    assert res["ok"] is True
    written = json.loads((tmp_path / "logs" / "v16.1-events.jsonl").read_text().splitlines()[-1])
    assert written["event"] == "f1_feedback" and written["call_id"] == "abc123"
    assert written["useful"] is False and written["note"] == "vago"


def test_append_label_rejects_empty_call_id(tmp_path: Path) -> None:
    """Sin call_id no escribe (fail-soft)."""
    assert L.append_label(tmp_path, "", useful=True)["ok"] is False


def test_read_hooks_wiring(tmp_path: Path) -> None:
    """read_hooks casa el cableado repo + global y cuenta disparos de la telemetría."""
    _make_log(tmp_path)
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"hooks": [{"command": "python dispatch.py"}]}]}}), encoding="utf-8")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
        "PreToolUse": [{"hooks": [{"command": "bash pre-bash-guard.sh"}]}]}}), encoding="utf-8")
    hk = L.read_hooks(tmp_path, home=home)
    assert hk["available"] is True
    pre = next(e for e in hk["events"] if e["event"] == "PreToolUse")
    assert "dispatch.py" in pre["repo"] and "pre-bash-guard.sh" in pre["global"]
    assert pre["wired"] is True
    assert hk["fired_by_source"]["mcp_server"] == 1


def test_read_hooks_handler_event_mapping(tmp_path: Path) -> None:
    """read_hooks cruza handler→evento: count y last_fired aparecen en el evento correcto.

    Crea telemetría con handlers confirmados de dos eventos distintos (Stop via
    post_agent_verify, PreToolUse via mcp_guard + migration_linter) y verifica que
    cada evento del ciclo de vida recibe el count agregado y el last_fired correcto.
    Los handlers sin mapeo (mcp_server) aparecen SOLO en fired_by_source, no en eventos.
    """
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "hooks").mkdir(exist_ok=True)
    (tmp_path / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")

    # 3 disparos de post_agent_verify → Stop; 2 de mcp_guard + 1 de migration_linter → PreToolUse
    # 1 de mcp_server → sin evento (bucket otros)
    lines = [
        json.dumps({"ts": "2026-06-01T10:00:00Z", "hook": "post_agent_verify"}),
        json.dumps({"ts": "2026-06-01T10:00:01Z", "hook": "post_agent_verify"}),
        json.dumps({"ts": "2026-06-01T10:00:05Z", "hook": "post_agent_verify"}),
        json.dumps({"ts": "2026-06-01T10:00:10Z", "hook": "mcp_guard"}),
        json.dumps({"ts": "2026-06-01T10:00:11Z", "hook": "mcp_guard"}),
        json.dumps({"ts": "2026-06-01T10:00:20Z", "hook": "migration_linter"}),
        json.dumps({"ts": "2026-06-01T10:00:30Z", "hook": "mcp_server"}),
    ]
    (tmp_path / "logs" / "v16.1-events.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    hk = L.read_hooks(tmp_path, home=home)

    events_by_name = {e["event"]: e for e in hk["events"]}

    # Stop: 3 disparos de post_agent_verify (stop.py:284,353)
    stop = events_by_name["Stop"]
    assert stop["count"] == 3, f"Stop count esperado 3, got {stop['count']}"
    assert stop["last_fired"] == "2026-06-01T10:00:05Z"

    # PreToolUse: 2 (mcp_guard) + 1 (migration_linter) = 3; last = migration_linter ts
    pre = events_by_name["PreToolUse"]
    assert pre["count"] == 3, f"PreToolUse count esperado 3, got {pre['count']}"
    assert pre["last_fired"] == "2026-06-01T10:00:20Z"

    # PostToolUse, SubagentStart, SessionStart, etc.: count=0, last_fired=''
    for ev_name in ("PostToolUse", "SubagentStart", "SessionStart", "SessionEnd"):
        ev = events_by_name[ev_name]
        assert ev["count"] == 0, f"{ev_name} count esperado 0, got {ev['count']}"
        assert ev["last_fired"] == ""

    # mcp_server no tiene evento → aparece en fired_by_source pero NO incrementa ningún evento
    assert hk["fired_by_source"]["mcp_server"] == 1
    # Ningún evento tiene count inflado por mcp_server
    assert all(
        events_by_name[n]["count"] == 0
        for n in ("UserPromptSubmit", "SubagentStop", "PreCompact")
    )
