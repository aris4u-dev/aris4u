"""Tests de regresión para los bugs clase-A round 2 (2026-06-29).

Cubre los 5 bugs nuevos detectados por el gate adversarial:
  Bug #1 — _discover_mcps incluye MCP observados en telemetría (origin='runtime').
  Bug #2 — read_hooks cuenta eventos con campo 'event' (no solo 'hook').
  Bug #3 — _health_mcp no duplica aris4u: total = servidores únicos.
  Bug #4 — read_memory.recent_decisions filtra provenance/fact con _REAL.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aris4u_console import live_data as L       # noqa: E402
from aris4u_console import capabilities as cap  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(repo: Path) -> None:
    """DB mínima con decisiones reales + provenance."""
    (repo / "data").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(repo / "data" / "sessions.db")
    conn.executescript("""
        CREATE TABLE decisions(
            decision TEXT, domain TEXT, locked INTEGER DEFAULT 0,
            created_at TEXT, client_id TEXT, mem_type TEXT
        );
        CREATE TABLE guards(pattern TEXT, prevention TEXT, severity TEXT,
            created_at TEXT, client_id TEXT);
        CREATE TABLE digests(date TEXT, summary TEXT, created_at TEXT, client_id TEXT);
        CREATE TABLE recall_feedback(recall_id TEXT PRIMARY KEY, useful INTEGER NOT NULL,
            marked_at TEXT);
    """)
    conn.execute("INSERT INTO decisions VALUES ('real A','arch',1,'2026-06-01','aris4u',NULL)")
    conn.execute("INSERT INTO decisions VALUES ('real B','db',0,'2026-06-02',NULL,'rule')")
    conn.execute("INSERT INTO decisions VALUES ('[commit abc] msg','git',0,'2026-06-29','x','provenance')")
    conn.execute("INSERT INTO decisions VALUES ('átomo Z','opt',0,'2026-06-28',NULL,'fact')")
    conn.commit()
    conn.close()


def _make_log(repo: Path, lines: list[str]) -> Path:
    (repo / "logs").mkdir(parents=True, exist_ok=True)
    p = repo / "logs" / "v16.1-events.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Bug #1 — _discover_mcps incluye MCP de telemetría
# ---------------------------------------------------------------------------

def test_discover_mcps_includes_runtime_from_telemetry(tmp_path: Path) -> None:
    """_mcp_from_telemetry devuelve servers de mcp_call no presentes en archivos."""
    lines = [
        json.dumps({"event": "mcp_call", "server": "claude-in-chrome", "tool": "navigate", "ts": "2026-06-01T10:00:00"}),
        json.dumps({"event": "mcp_call", "server": "claude_ai_Google_Drive", "tool": "search_files", "ts": "2026-06-02T10:00:00"}),
        json.dumps({"event": "depth_inject", "intent": "code", "ts": "2026-06-01T11:00:00"}),
    ]
    _make_log(tmp_path, lines)
    entries = L._mcp_from_telemetry(tmp_path)
    names = [e["name"] for e in entries]
    assert "claude-in-chrome" in names, f"claude-in-chrome no encontrado en: {names}"
    assert "Google Drive" in names, f"'Google Drive' (normalizado) no encontrado en: {names}"
    # El entry de telemetría tiene origin='runtime'
    for e in entries:
        assert e["origin"] == "runtime", f"origin incorrecto: {e}"


def test_discover_mcps_normalizes_claude_ai_prefix(tmp_path: Path) -> None:
    """claude_ai_Intuit_QuickBooks se normaliza a 'Intuit QuickBooks' (igual que /cap/mcp)."""
    lines = [
        json.dumps({"event": "mcp_call", "server": "claude_ai_Intuit_QuickBooks", "tool": "company_info", "ts": "2026-06-01T10:00:00"}),
    ]
    _make_log(tmp_path, lines)
    entries = L._mcp_from_telemetry(tmp_path)
    names = [e["name"] for e in entries]
    assert "Intuit QuickBooks" in names, f"nombre normalizado ausente, got: {names}"
    assert "claude_ai_Intuit_QuickBooks" not in names, "nombre sin normalizar no debe aparecer"


def test_discover_mcps_no_log_returns_empty(tmp_path: Path) -> None:
    """_mcp_from_telemetry devuelve [] si el log no existe (fail-soft)."""
    entries = L._mcp_from_telemetry(tmp_path)
    assert entries == []


def test_discover_mcps_deduplicates_telemetry_with_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Servers en archivos Y en telemetría no se duplican en _discover_mcps."""
    lines = [
        # aris4u y supabase ya están en ~/.claude.json → no deben duplicarse
        json.dumps({"event": "mcp_call", "server": "aris4u", "tool": "aris_search", "ts": "2026-06-01T10:00:00"}),
        json.dumps({"event": "mcp_call", "server": "ide", "tool": "x", "ts": "2026-06-01T11:00:00"}),
    ]
    _make_log(tmp_path, lines)
    # Simular ~/.claude.json con aris4u ya cableado
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"aris4u": {"command": "python3", "args": []}}})
    )
    (fake_home / ".claude" / "plugins").mkdir(parents=True)
    result = L._discover_mcps(home=fake_home, repo=tmp_path)
    global_names = result["mcp_global"]
    # aris4u aparece exactamente una vez (de archivos, no de telemetría)
    assert global_names.count("aris4u") == 1, f"aris4u duplicado en mcp_global: {global_names}"
    # ide viene de telemetría
    assert "ide" in global_names, f"'ide' de telemetría ausente: {global_names}"


# ---------------------------------------------------------------------------
# Bug #2 — read_hooks cuenta eventos 'event' (no solo 'hook')
# ---------------------------------------------------------------------------

def test_read_hooks_counts_auto_recall_as_user_prompt_submit(tmp_path: Path) -> None:
    """auto_recall (event=, hook=None) se cuenta como disparo de UserPromptSubmit."""
    lines = [
        json.dumps({"ts": "2026-06-01T10:00:00", "event": "auto_recall", "hook": None, "query": "test"}),
        json.dumps({"ts": "2026-06-01T10:01:00", "event": "auto_recall", "hook": None, "query": "test2"}),
        json.dumps({"ts": "2026-06-01T10:02:00", "event": "depth_inject", "hook": "depth_inject"}),
    ]
    _make_log(tmp_path, lines)
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}))
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))

    hk = L.read_hooks(tmp_path, home=fake_home)
    ups_row = next((e for e in hk["events"] if e["event"] == "UserPromptSubmit"), None)
    assert ups_row is not None, "UserPromptSubmit no encontrado en events"
    assert ups_row["count"] == 2, (
        f"UserPromptSubmit debe contar 2 auto_recall, got count={ups_row['count']}"
    )


def test_read_hooks_counts_session_briefing_as_session_start(tmp_path: Path) -> None:
    """session_briefing (event=, hook=None) se cuenta como SessionStart."""
    lines = [
        json.dumps({"ts": "2026-06-01T09:00:00", "event": "session_briefing", "hook": None}),
    ]
    _make_log(tmp_path, lines)
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}))
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))

    hk = L.read_hooks(tmp_path, home=fake_home)
    ss_row = next((e for e in hk["events"] if e["event"] == "SessionStart"), None)
    assert ss_row is not None
    assert ss_row["count"] == 1, f"SessionStart debe contar 1, got {ss_row['count']}"


def test_read_hooks_counts_session_end_dirty_check_as_session_end(tmp_path: Path) -> None:
    """session_end_dirty_check (event=, hook=None) se cuenta como SessionEnd."""
    lines = [
        json.dumps({"ts": "2026-06-01T23:00:00", "event": "session_end_dirty_check", "hook": None}),
    ]
    _make_log(tmp_path, lines)
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}))
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))

    hk = L.read_hooks(tmp_path, home=fake_home)
    se_row = next((e for e in hk["events"] if e["event"] == "SessionEnd"), None)
    assert se_row is not None
    assert se_row["count"] == 1, f"SessionEnd debe contar 1, got {se_row['count']}"


def test_read_hooks_events_with_hook_field_still_counted(tmp_path: Path) -> None:
    """Eventos con campo 'hook' siguen contándose vía _HANDLER_TO_EVENT (no roto)."""
    lines = [
        json.dumps({"ts": "2026-06-01T10:00:00", "event": "guard_run", "hook": "mcp_guard"}),
        json.dumps({"ts": "2026-06-01T10:01:00", "event": "guard_run", "hook": "phi_guard"}),
    ]
    _make_log(tmp_path, lines)
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}))
    fake_home = tmp_path / "home"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}))

    hk = L.read_hooks(tmp_path, home=fake_home)
    ptu_row = next((e for e in hk["events"] if e["event"] == "PreToolUse"), None)
    assert ptu_row is not None
    assert ptu_row["count"] == 2, f"PreToolUse debe contar 2 (mcp_guard+phi_guard), got {ptu_row['count']}"


# ---------------------------------------------------------------------------
# Bug #3 — _health_mcp no duplica aris4u
# ---------------------------------------------------------------------------

def test_health_mcp_no_duplicate_aris4u(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cuando aris4u está en _local_mcp_servers, _health_mcp no lo añade de nuevo."""
    fake_servers = {
        "aris4u":      {"command": "/path/to/mcp_wrapper.sh", "args": []},
        "context7":    {"command": "npx", "args": ["-y", "@upstash/context7-mcp"]},
    }
    monkeypatch.setattr(cap, "_local_mcp_servers", lambda: fake_servers)
    # aris_mcp_tools debe devolver tools (para que el enriquecimiento se active)
    monkeypatch.setattr(cap, "_aris_mcp_tools", lambda repo: ["aris_search", "aris_ingest"])

    results = cap._health_mcp(tmp_path)
    aris_results = [r for r in results if r["name"] == "aris4u"]
    assert len(aris_results) == 1, (
        f"aris4u debe aparecer exactamente 1 vez, got {len(aris_results)}: {aris_results}"
    )
    # El detail del único entry enriquece con las tools
    assert "tools" in aris_results[0]["detail"], (
        f"detail de aris4u debe incluir info de tools: {aris_results[0]['detail']}"
    )


def test_health_mcp_total_unique_servers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """summary.total == número de servidores únicos (sin contar aris4u dos veces)."""
    fake_servers = {
        "aris4u":    {"command": "/path/mcp_wrapper.sh"},
        "stripe":    {"url": "https://mcp.stripe.com", "command": ""},
        "context7":  {"command": "npx", "args": []},
    }
    monkeypatch.setattr(cap, "_local_mcp_servers", lambda: fake_servers)
    monkeypatch.setattr(cap, "_aris_mcp_tools", lambda repo: ["aris_search"])
    # Fix #2 (7º gate): _health_mcp ahora también itera plugins y telemetría — aislar el test.
    monkeypatch.setattr(cap.live_data, "_mcp_from_plugin_cache", lambda p: [])
    monkeypatch.setattr(cap.live_data, "_mcp_from_local_plugins", lambda p: [])
    monkeypatch.setattr(cap.live_data, "_mcp_from_telemetry", lambda repo: [])

    results = cap._health_mcp(tmp_path)
    names = [r["name"] for r in results]
    # 3 servidores únicos: aris4u, stripe, context7
    assert len(names) == 3, f"esperado 3 únicos, got {len(names)}: {names}"
    assert names.count("aris4u") == 1


# ---------------------------------------------------------------------------
# Bug #4 — read_memory.recent_decisions filtra provenance/fact
# ---------------------------------------------------------------------------

def test_read_memory_recent_decisions_excludes_provenance(tmp_path: Path) -> None:
    """recent_decisions en read_memory no devuelve filas provenance."""
    _make_db(tmp_path)
    result = L.read_memory(tmp_path)
    assert result["available"] is True
    rd = result["recent_decisions"]
    # Ninguna debe empezar con '[commit' (indicador de provenance)
    bad = [d["decision"] for d in rd if (d.get("decision") or "").startswith("[commit")]
    assert bad == [], f"recent_decisions incluye provenance: {bad}"


def test_read_memory_recent_decisions_returns_real_only(tmp_path: Path) -> None:
    """recent_decisions contiene solo las decisiones reales (mem_type NULL o not prov/fact)."""
    _make_db(tmp_path)
    result = L.read_memory(tmp_path)
    rd = result["recent_decisions"]
    # El DB de test tiene 2 decisiones reales ('real A' y 'real B') y 2 provenance/fact
    assert len(rd) == 2, f"expected 2 real decisions, got {len(rd)}: {[d['decision'] for d in rd]}"
    decisions_text = {d["decision"] for d in rd}
    assert "real A" in decisions_text
    assert "real B" in decisions_text


def test_read_memory_recent_decisions_most_recent_first(tmp_path: Path) -> None:
    """recent_decisions está ordenada por created_at DESC (más reciente primero)."""
    _make_db(tmp_path)
    result = L.read_memory(tmp_path)
    rd = result["recent_decisions"]
    if len(rd) >= 2:
        assert rd[0]["created_at"] >= rd[1]["created_at"], (
            "recent_decisions debe estar en orden DESC por created_at"
        )
