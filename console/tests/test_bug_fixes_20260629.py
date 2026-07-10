"""Tests de regresión para los 6 bugs clase-A resueltos el 2026-06-29.

Cubre:
  Fix #1 — search_memory/_search_decisions aplica _REAL por defecto.
  Fix #2 — read_amplifier lee el log completo (no solo tail).
  Fix #3 — _health_mcp no marca remotos como FAIL por binario.
  Fix #4 — _st_memory usa MAX(created_at) con filtro _REAL.
  Fix #5 — read_hooks expone window_lines/window en el payload.
  Fix #6 — _st_hooks usa MAX(mtime hooks.json, settings.json).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aris4u_console import live_data as L  # noqa: E402
from aris4u_console import capabilities as cap  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(repo: Path, *, extra_rows: list[tuple] | None = None) -> None:
    """DB mínima con decisiones reales + provenance + fact."""
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
    # Decisiones reales (mem_type NULL o not provenance/fact)
    conn.execute("INSERT INTO decisions VALUES ('decid A','arch',1,'2026-06-10','aris4u',NULL)")
    conn.execute("INSERT INTO decisions VALUES ('decid B','db',0,'2026-06-11',NULL,'rule')")
    # Ruido que _REAL debe excluir
    conn.execute("INSERT INTO decisions VALUES ('[commit] x','git',0,'2026-06-29','aris4u','provenance')")
    conn.execute("INSERT INTO decisions VALUES ('átomo Y','optimization',0,'2026-06-28',NULL,'fact')")
    for row in (extra_rows or []):
        conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?,?)", row)
    conn.commit()
    conn.close()


def _make_log(repo: Path, lines: list[str]) -> Path:
    """Escribe un log jsonl en el repo y devuelve su path."""
    (repo / "logs").mkdir(parents=True, exist_ok=True)
    p = repo / "logs" / "v16.1-events.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Fix #1 — search_memory aplica filtro _REAL por defecto
# ---------------------------------------------------------------------------

def test_search_memory_default_excludes_provenance(tmp_path: Path) -> None:
    """Con mem_type='' (default), search_memory NO devuelve filas provenance/fact."""
    _make_db(tmp_path)
    result = L.search_memory(tmp_path, q="", limit=50)
    assert result["available"] is True
    bad = [d for d in result["decisions"] if d.get("mem_type") in ("provenance", "fact")]
    assert bad == [], f"search_memory devolvió provenance/fact: {bad}"


def test_search_memory_default_returns_real_decisions(tmp_path: Path) -> None:
    """Con default, devuelve las filas reales (NULL y 'rule')."""
    _make_db(tmp_path)
    result = L.search_memory(tmp_path, q="", limit=50)
    decisions = result["decisions"]
    assert len(decisions) == 2  # 'decid A' (NULL) y 'decid B' (rule)
    types = {d["mem_type"] for d in decisions}
    assert types == {"(none)", "rule"}  # (none) = mem_type NULL mostrado como (none)


def test_search_memory_explicit_memtype_overrides_real_filter(tmp_path: Path) -> None:
    """Con mem_type='fact' explícito, SÍ devuelve facts (toggle funciona)."""
    _make_db(tmp_path)
    result = L.search_memory(tmp_path, q="", mem_type="fact", limit=50)
    assert result["available"] is True
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["mem_type"] == "fact"


# ---------------------------------------------------------------------------
# Fix #2 — read_amplifier lee el log completo (no tail)
# ---------------------------------------------------------------------------

def test_read_amplifier_finds_f1_beyond_tail_window(tmp_path: Path) -> None:
    """F1 events al inicio del log se encuentran aunque estén fuera de un tail corto.

    Creamos un log con 110 líneas: F1 en la posición 1, luego 109 fillers.
    Un tail de 100 líneas no alcanzaría la primera línea; la lectura completa sí.
    """
    f1_line = json.dumps({
        "event": "mcp_tool", "tool": "aris_structure", "call_id": "c_early",
        "available": True, "latency_ms": 5000, "ts": "2026-01-01T00:00:00",
        "backend": "mlx", "chars": 200,
    })
    filler = json.dumps({"event": "depth_inject", "intent": "code"})
    lines = [f1_line] + [filler] * 109  # F1 en pos 1, 109 fillers
    _make_log(tmp_path, lines)

    amp = L.read_amplifier(tmp_path)
    assert amp["available"] is True
    # La lectura completa encuentra el único F1 call
    assert amp["calls"] == 1, f"expected 1 F1 call but got {amp['calls']}"
    assert amp["availability_rate"] == 1.0


def test_read_amplifier_no_window_param() -> None:
    """read_amplifier ya no acepta el parámetro 'window' (firma limpiada)."""
    import inspect
    sig = inspect.signature(L.read_amplifier)
    assert "window" not in sig.parameters, (
        "read_amplifier no debería tener parámetro 'window' — el fix lo eliminó"
    )


# ---------------------------------------------------------------------------
# Fix #3 — _health_mcp no marca remotos como FAIL
# ---------------------------------------------------------------------------

def test_health_mcp_remote_server_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Connectors HTTP/OAuth (command='') se marcan ok, no FAIL."""
    fake_servers = {
        "stripe":           {"url": "https://mcp.stripe.com", "command": ""},
        "cloudflare-builds": {"url": "https://builds.mcp.cloudflare.com/mcp"},
        "local-tool":       {"command": "npx", "args": ["-y", "some-mcp"]},
    }
    monkeypatch.setattr(cap, "_local_mcp_servers", lambda: fake_servers)

    (tmp_path / "integrations").mkdir(parents=True)
    results = cap._health_mcp(tmp_path)

    stripe = next(r for r in results if r["name"] == "stripe")
    cf = next(r for r in results if r["name"] == "cloudflare-builds")
    local = next(r for r in results if r["name"] == "local-tool")

    assert stripe["ok"] is True, f"stripe debería ser ok: {stripe['detail']}"
    assert "remoto" in stripe["detail"]
    assert cf["ok"] is True, f"cloudflare-builds debería ser ok: {cf['detail']}"
    assert "remoto" in cf["detail"]
    # El local con binario real: npx debería encontrarse en PATH
    # (toleramos si no está instalado en el entorno de test)
    assert isinstance(local["ok"], bool)


# ---------------------------------------------------------------------------
# Fix #4 — _st_memory usa MAX(created_at) con filtro _REAL
# ---------------------------------------------------------------------------

def test_st_memory_date_not_polluted_by_provenance(tmp_path: Path) -> None:
    """La fecha 'updated' de Memoria refleja la decisión REAL más reciente, no provenance."""
    # provenance tiene created_at '2026-06-29' (más reciente) que las reales (2026-06-11)
    _make_db(tmp_path)
    conn = sqlite3.connect(tmp_path / "data" / "sessions.db")
    conn.execute("INSERT INTO decisions VALUES ('prov Z','git',0,'2026-06-29','x','provenance')")
    conn.commit()
    conn.close()

    # Simular _st_memory via read_status
    status = L.read_status(tmp_path)
    mem_item = next(i for i in status["items"] if i["name"] == "Memoria")
    # '2026-06-11' = max de decisiones reales; '2026-06-29' = provenance (ruido)
    # La fecha mostrada debe ser la de la decisión real más reciente
    assert mem_item["updated"] == "11/06/2026", (
        f"updated debería ser 11/06/2026 (real) pero es {mem_item['updated']!r}"
    )


# ---------------------------------------------------------------------------
# Fix #5 — read_hooks expone window_lines y window
# ---------------------------------------------------------------------------

def test_read_hooks_exposes_window(tmp_path: Path) -> None:
    """read_hooks devuelve window_lines (tamaño configurado) y window (eventos parseados)."""
    _make_log(tmp_path, [
        json.dumps({"ts": "2026-06-01T10:00:00", "event": "depth_inject", "hook": "depth_inject"}),
    ])
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")

    hk = L.read_hooks(tmp_path, home=home)
    assert hk["available"] is True
    assert "window_lines" in hk, "read_hooks debe exponer 'window_lines'"
    assert "window" in hk, "read_hooks debe exponer 'window'"
    assert hk["window_lines"] == L._HOOKS_WINDOW_LINES
    assert isinstance(hk["window"], int) and hk["window"] >= 0


def test_read_hooks_window_when_events_passed(tmp_path: Path) -> None:
    """Cuando events se pasa externamente, window_lines es None (no gestionado aquí)."""
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")

    hk = L.read_hooks(tmp_path, home=home, events=[])
    assert hk["window_lines"] is None  # el caller gestiona la ventana
    assert hk["window"] == 0


# ---------------------------------------------------------------------------
# Fix #6 — _st_hooks usa MAX(mtime hooks.json, settings.json)
# ---------------------------------------------------------------------------

def test_st_hooks_updated_uses_settings_when_newer(tmp_path: Path) -> None:
    """Cuando settings.json es más reciente que hooks.json, 'updated' refleja settings.json."""
    # FIX #9 (round 2): skip si no existe settings.json en el entorno (CI sin config local).
    if not (Path.home() / ".claude" / "settings.json").is_file():
        pytest.skip("~/.claude/settings.json no existe — skip en CI sin config local")
    (tmp_path / "hooks").mkdir()
    hj = tmp_path / "hooks" / "hooks.json"
    hj.write_text(json.dumps({"hooks": {"PreToolUse": []}}), encoding="utf-8")

    # Tocar hooks.json en el pasado (via utime)
    import os
    import time
    old_ts = time.time() - 86400 * 20  # 20 días atrás
    os.utime(hj, (old_ts, old_ts))

    # settings.json 10 días atrás (más reciente que hooks.json pero más antiguo que hoy)
    settings_json = Path.home() / ".claude" / "settings.json"
    if settings_json.is_file():
        sj_mtime = settings_json.stat().st_mtime
        hj_mtime = hj.stat().st_mtime
        expected_date = datetime.fromtimestamp(max(hj_mtime, sj_mtime)).strftime("%d/%m/%Y")

        status = L.read_status(tmp_path)
        hooks_item = next(i for i in status["items"] if i["name"] == "Hooks (reflejos)")
        assert hooks_item["updated"] == expected_date, (
            f"updated={hooks_item['updated']!r}, esperado={expected_date!r} "
            f"(max de hooks.json={datetime.fromtimestamp(hj_mtime).strftime('%d/%m/%Y')}, "
            f"settings.json={datetime.fromtimestamp(sj_mtime).strftime('%d/%m/%Y')})"
        )
