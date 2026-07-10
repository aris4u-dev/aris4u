"""Tests for the read-only ARIS4U CLI panels (tools/aris_status.py, tools/aris_config.py).

Cobertura de los dos visores de la Capa 0 del wrapper Desktop:

  - aris_status: panel de capacidades (settings.json + sessions.db mode=ro + telemetría JSONL).
  - aris_config: visor de config (modelo, env/flags, MCP global vs repo, duplicados).

REGLA SAGRADA (conftest._isolate_sessions_db / _isolate_event_log): jamás tocar DBs ni
logs reales. Aquí TODOS los path-constants de ambos módulos se monkeypatchean a tmp_path,
y cada test que pudiera escribir (sólo aris_config.set_model) verifica que el
~/.claude/settings.json REAL nunca cambia.

Patrón de import = directo (tests/tools): se importan los módulos y se ejercitan
collect()/render()/main() llamando a las funciones, no por subprocess.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import tools.aris_config as cfg
import tools.aris_status as status


# ---------------------------------------------------------------------------
# Helpers para fabricar fixtures aisladas (settings.json, sessions.db, log).
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data))
    return path


def _make_sessions_db(path: Path, *, with_tables: bool = True) -> Path:
    """Crea un sessions.db mínimo con las tablas que db_counts() consulta."""
    con = sqlite3.connect(str(path))
    try:
        if with_tables:
            con.execute(
                "CREATE TABLE decisions (id INTEGER PRIMARY KEY, client_id TEXT, body TEXT)"
            )
            con.execute("CREATE TABLE guards (id INTEGER PRIMARY KEY, body TEXT)")
            con.execute("CREATE TABLE digests (id INTEGER PRIMARY KEY, summary TEXT)")
            con.executemany(
                "INSERT INTO decisions (client_id, body) VALUES (?, ?)",
                [("client-b", "a"), ("client-b", "b"), ("client-c", "c"), (None, "d")],
            )
            con.executemany(
                "INSERT INTO guards (body) VALUES (?)", [("g1",), ("g2",)]
            )
            con.execute("INSERT INTO digests (summary) VALUES ('s1')")
        con.commit()
    finally:
        con.close()
    return path


SAMPLE_SETTINGS: dict[str, Any] = {
    "model": "claude-opus-4-8",
    "env": {
        "ARIS4U_HEALTHCARE": "0",
        "ARIS4U_AUTOUPDATE": "shadow",
        "ENABLE_PROMPT_CACHING_1H": "true",
        "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY": "4",
        "UNRELATED_KNOB": "ignore-me",
    },
    "mcpServers": {"aris4u": {}, "context7": {}, "supabase": {}},
    "hooks": {
        "PreToolUse": [
            {
                "hooks": [
                    {"command": 'bash "/x/aris4u/hooks/type-hints-guard.sh"'},
                    {"command": 'bash "/x/aris4u/hooks/supabase-rls-guard.sh"'},
                    {"command": ".venv312/bin/python3 hooks/dispatch.py PreToolUse"},
                ]
            }
        ],
        "PostToolUse": [
            {"hooks": [{"command": 'bash "/x/aris4u/hooks/phi_guard.sh"'}]}
        ],
        "SessionStart": [],  # evento sin hooks → no debe aparecer en by_event
    },
}


@pytest.fixture
def isolated_status(tmp_path, monkeypatch):
    """Apunta TODOS los path-constants de aris_status a archivos tmp."""
    settings = _write_json(tmp_path / "settings.json", SAMPLE_SETTINGS)
    db = _make_sessions_db(tmp_path / "sessions.db")
    log = tmp_path / "events.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"event": "auto_recall", "ts": "2026-06-18T10:00:00Z"}),
                "   ",  # línea en blanco → debe ignorarse
                "{not valid json",  # línea corrupta → debe ignorarse
                json.dumps({"hook": "PreToolUse", "timestamp": "2026-06-18T11:00:00Z"}),
            ]
        )
        + "\n"
    )
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    _write_json(plugin_dir / "plugin.json", {"version": "16.9.0"})

    monkeypatch.setattr(status, "SETTINGS", settings)
    monkeypatch.setattr(status, "SESSIONS_DB", db)
    monkeypatch.setattr(status, "EVENTS_LOG", log)
    monkeypatch.setattr(status, "ARIS_ROOT", tmp_path)
    return {"settings": settings, "db": db, "log": log, "root": tmp_path}


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Apunta los path-constants de aris_config a archivos tmp."""
    settings = _write_json(tmp_path / "settings.json", SAMPLE_SETTINGS)
    repo_mcp = _write_json(
        tmp_path / ".mcp.json",
        {"mcpServers": {"aris4u": {}, "figma": {}}},  # aris4u duplicado vs global
    )
    monkeypatch.setattr(cfg, "SETTINGS", settings)
    monkeypatch.setattr(cfg, "REPO_MCP", repo_mcp)
    return {"settings": settings, "repo_mcp": repo_mcp}


# ===========================================================================
# aris_status — lectura de settings.json
# ===========================================================================


class TestStatusSettings:
    def test_load_settings_ok(self, isolated_status):
        s = status.load_settings()
        assert s["model"] == "claude-opus-4-8"
        assert "aris4u" in s["mcpServers"]

    def test_load_settings_missing_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(status, "SETTINGS", tmp_path / "nope.json")
        assert status.load_settings() == {}

    def test_load_settings_broken_json_returns_empty(self, tmp_path, monkeypatch):
        broken = tmp_path / "settings.json"
        broken.write_text("{ this is : not json ]")
        monkeypatch.setattr(status, "SETTINGS", broken)
        assert status.load_settings() == {}

    def test_hook_summary_counts_and_guards(self, isolated_status):
        by_event, guards, total = status.hook_summary(SAMPLE_SETTINGS)
        # 3 PreToolUse + 1 PostToolUse = 4; SessionStart vacío no cuenta.
        assert total == 4
        assert by_event["PreToolUse"] == 3
        assert by_event["PostToolUse"] == 1
        assert "SessionStart" not in by_event
        # Guards = comandos con 'aris4u' + un GUARD_MARKER ('guard'/'phi_'/...).
        assert "type-hints-guard" in guards
        assert "supabase-rls-guard" in guards
        assert "phi_guard" in guards
        # dispatch.py no es guard (no termina en .sh ni matchea marker de guard).
        assert "dispatch" not in guards
        assert guards == sorted(set(guards))  # ordenado y deduplicado

    def test_hook_summary_no_hooks(self):
        by_event, guards, total = status.hook_summary({})
        assert (by_event, guards, total) == ({}, [], 0)

    def test_mcp_servers_sorted(self, isolated_status):
        assert status.mcp_servers(SAMPLE_SETTINGS) == ["aris4u", "context7", "supabase"]

    def test_mcp_servers_empty(self):
        assert status.mcp_servers({}) == []

    def test_script_name_extraction(self):
        assert status._script_name('bash "/a/b/phi_guard.sh"') == "phi_guard"
        # Sin .sh: cae al último segmento de ruta.
        assert status._script_name("/usr/bin/python3") == "python3"

    def test_color_toggle(self):
        assert status._color("32", "hi", False) == "hi"
        assert status._color("32", "hi", True) == "\033[32mhi\033[0m"


# ===========================================================================
# aris_status — lectura de sessions.db (read-only) + telemetría
# ===========================================================================


class TestStatusDb:
    def test_db_counts_ok(self, isolated_status):
        c = status.db_counts()
        assert c["ok"] is True
        assert c["decisions"] == 4
        assert c["guards"] == 2
        assert c["digests"] == 1
        by_client = dict(c["by_client"])
        assert by_client["client-b"] == 2
        assert by_client["client-c"] == 1
        assert by_client["(none)"] == 1  # COALESCE de client_id NULL

    def test_db_counts_missing_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(status, "SESSIONS_DB", tmp_path / "absent.db")
        out = status.db_counts()
        assert out == {"ok": False}

    def test_db_counts_corrupt_db_fails_soft(self, tmp_path, monkeypatch):
        """DB sin las tablas esperadas → ok=False + 'error', sin crash."""
        bad = _make_sessions_db(tmp_path / "empty.db", with_tables=False)
        monkeypatch.setattr(status, "SESSIONS_DB", bad)
        out = status.db_counts()
        assert out["ok"] is False
        assert "error" in out

    def test_db_opened_read_only(self, isolated_status):
        """El handle es mode=ro: una escritura debe ser rechazada por SQLite."""
        uri = f"file:{isolated_status['db']}?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError):
                con.execute("INSERT INTO guards (body) VALUES ('x')")
                con.commit()
        finally:
            con.close()

    def test_tail_events_parses_and_skips_junk(self, isolated_status):
        events = status.tail_events()
        # 2 líneas válidas; en blanco y corrupta descartadas.
        assert len(events) == 2
        assert events[0]["event"] == "auto_recall"
        assert events[1]["hook"] == "PreToolUse"

    def test_tail_events_limit(self, isolated_status):
        assert len(status.tail_events(n=1)) == 1

    def test_tail_events_missing_log(self, tmp_path, monkeypatch):
        monkeypatch.setattr(status, "EVENTS_LOG", tmp_path / "no-log.jsonl")
        assert status.tail_events() == []


# ===========================================================================
# aris_status — collect / render / main (no escriben nada)
# ===========================================================================


class TestStatusCollectRenderMain:
    def test_collect_structure(self, isolated_status):
        data = status.collect()
        assert data["version"] == "16.9.0"
        assert data["hooks"]["total"] == 4
        assert data["mcp"] == ["aris4u", "context7", "supabase"]
        assert data["memory"]["ok"] is True
        assert data["model_default"] == "claude-opus-4-8"
        # env se filtra a ARIS4U*/CLAUDE_CODE*, el UNRELATED_KNOB se descarta.
        assert "ARIS4U_HEALTHCARE" in data["env"]
        assert "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY" in data["env"]
        assert "UNRELATED_KNOB" not in data["env"]
        assert "ENABLE_PROMPT_CACHING_1H" not in data["env"]  # no empieza por prefijos

    def test_plugin_version_missing_returns_qmark(self, tmp_path, monkeypatch):
        monkeypatch.setattr(status, "ARIS_ROOT", tmp_path)  # sin .claude-plugin
        assert status._plugin_version() == "?"

    def test_render_no_color_is_plain(self, isolated_status):
        data = status.collect()
        out = status.render(data, color=False)
        assert "\033[" not in out  # sin códigos ANSI
        assert "ARIS4U v16.9.0" in out
        assert "HOOKS:" in out
        assert "MCP servers: 3" in out
        assert "MEMORIA:" in out

    def test_render_color_has_ansi(self, isolated_status):
        out = status.render(status.collect(), color=True)
        assert "\033[" in out

    def test_render_memory_warn_when_unreadable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(status, "SESSIONS_DB", tmp_path / "absent.db")
        # collect() necesita settings/version válidos → reusar el root tmp.
        monkeypatch.setattr(status, "SETTINGS", tmp_path / "no-settings.json")
        monkeypatch.setattr(status, "EVENTS_LOG", tmp_path / "no-log.jsonl")
        monkeypatch.setattr(status, "ARIS_ROOT", tmp_path)
        out = status.render(status.collect(), color=False)
        assert "MEMORIA: sessions.db no legible" in out

    def test_render_warns_non_claude_model(self, tmp_path, monkeypatch):
        settings = _write_json(tmp_path / "settings.json", {"model": "gpt-4o"})
        monkeypatch.setattr(status, "SETTINGS", settings)
        monkeypatch.setattr(status, "SESSIONS_DB", tmp_path / "absent.db")
        monkeypatch.setattr(status, "EVENTS_LOG", tmp_path / "no-log.jsonl")
        monkeypatch.setattr(status, "ARIS_ROOT", tmp_path)
        # Modelo no-claude → ruta del warn (amarillo) sin crash.
        out = status.render(status.collect(), color=True)
        assert "MODELO por defecto" in out

    def test_main_default_prints_panel(self, isolated_status, capsys):
        rc = status.main(["--no-color"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "STATUS DE CAPACIDADES" in captured.out

    def test_main_json_is_parseable(self, isolated_status, capsys):
        rc = status.main(["--json"])
        captured = capsys.readouterr()
        assert rc == 0
        parsed = json.loads(captured.out)
        assert parsed["version"] == "16.9.0"
        assert parsed["mcp"] == ["aris4u", "context7", "supabase"]

    def test_status_does_not_write_anything(self, isolated_status):
        """Invariante read-only: ni el panel ni el JSON tocan los archivos fuente."""
        before = {
            p: isolated_status[p].stat().st_mtime if isolated_status[p].exists() else None
            for p in ("settings", "db", "log")
        }
        before_sizes = {
            p: isolated_status[p].stat().st_size for p in ("settings", "db", "log")
        }
        status.main(["--no-color"])
        status.main(["--json"])
        for p in ("settings", "db", "log"):
            assert isolated_status[p].stat().st_mtime == before[p]
            assert isolated_status[p].stat().st_size == before_sizes[p]


# ===========================================================================
# aris_config — collect / render / main / set_model
# ===========================================================================


class TestConfigCollect:
    def test_collect_structure_and_duplicates(self, isolated_config):
        data = cfg.collect()
        assert data["model_default"] == "claude-opus-4-8"
        assert "aris4u" in data["mcp_global"]
        assert "figma" in data["mcp_repo"]
        # aris4u está en global Y en .mcp.json del repo → duplicado detectado.
        assert data["mcp_duplicated"] == ["aris4u"]
        assert data["settings_path"] == str(isolated_config["settings"])
        # Sólo se proyectan los ENV_KNOBS conocidos; los presentes traen su valor.
        assert set(data["env"]) == set(cfg.ENV_KNOBS)
        assert data["env"]["ARIS4U_HEALTHCARE"] == "0"
        assert data["env"]["ARIS4U_AUTOUPDATE"] == "shadow"
        # Knob no fijado en settings → placeholder, nunca KeyError.
        assert data["env"]["ARIS4U_VALIDATION_LOG"] == "(no fijado)"

    def test_collect_missing_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cfg, "SETTINGS", tmp_path / "no-settings.json")
        monkeypatch.setattr(cfg, "REPO_MCP", tmp_path / "no-mcp.json")
        data = cfg.collect()
        assert data["model_default"] is None
        assert data["mcp_global"] == []
        assert data["mcp_repo"] == []
        assert data["mcp_duplicated"] == []
        # env sigue completo (todos los knobs como '(no fijado)').
        assert all(v == "(no fijado)" for v in data["env"].values())

    def test_collect_no_duplicates(self, tmp_path, monkeypatch):
        settings = _write_json(
            tmp_path / "settings.json", {"mcpServers": {"aris4u": {}}}
        )
        repo = _write_json(tmp_path / ".mcp.json", {"mcpServers": {"figma": {}}})
        monkeypatch.setattr(cfg, "SETTINGS", settings)
        monkeypatch.setattr(cfg, "REPO_MCP", repo)
        assert cfg.collect()["mcp_duplicated"] == []

    def test_load_json_broken_returns_empty(self, tmp_path):
        bad = tmp_path / "x.json"
        bad.write_text("not json at all {{{")
        assert cfg.load_json(bad) == {}

    def test_load_json_missing_returns_empty(self, tmp_path):
        assert cfg.load_json(tmp_path / "ghost.json") == {}


class TestConfigRenderMain:
    def test_render_with_duplicates(self, isolated_config):
        out = cfg.render(cfg.collect())
        assert "CONFIGURACIÓN EFECTIVA" in out
        assert "MODELO por defecto : claude-opus-4-8" in out
        assert "DUPLICADOS" in out  # bloque de aviso de duplicados
        assert "ARIS4U_HEALTHCARE" in out

    def test_render_no_model_uses_placeholder(self, tmp_path, monkeypatch):
        settings = _write_json(tmp_path / "settings.json", {})
        monkeypatch.setattr(cfg, "SETTINGS", settings)
        monkeypatch.setattr(cfg, "REPO_MCP", tmp_path / "no-mcp.json")
        out = cfg.render(cfg.collect())
        assert "no fijado" in out
        assert "DUPLICADOS" not in out  # sin MCP → sin duplicados

    def test_main_default_prints_table(self, isolated_config, capsys):
        rc = cfg.main([])
        captured = capsys.readouterr()
        assert rc == 0
        assert "CONFIGURACIÓN EFECTIVA" in captured.out

    def test_main_json_parseable(self, isolated_config, capsys):
        rc = cfg.main(["--json"])
        captured = capsys.readouterr()
        assert rc == 0
        parsed = json.loads(captured.out)
        assert parsed["model_default"] == "claude-opus-4-8"
        assert parsed["mcp_duplicated"] == ["aris4u"]

    def test_main_set_model_without_arg_errors(self, isolated_config, capsys):
        rc = cfg.main(["--set-model"])
        captured = capsys.readouterr()
        assert rc == 2
        assert "requiere un id de modelo" in captured.out

    def test_config_read_paths_do_not_write(self, isolated_config):
        """collect/render/main(--json) son lectura pura sobre settings + .mcp.json."""
        files = (isolated_config["settings"], isolated_config["repo_mcp"])
        before = {f: (f.stat().st_mtime, f.stat().st_size) for f in files}
        cfg.main([])
        cfg.main(["--json"])
        for f in files:
            assert (f.stat().st_mtime, f.stat().st_size) == before[f]


class TestConfigSetModel:
    """set_model SÍ escribe — pero sólo sobre el settings.json AISLADO."""

    def test_set_model_writes_isolated_and_backs_up(self, isolated_config):
        settings = isolated_config["settings"]
        msg = cfg.set_model("claude-sonnet-4-7")
        assert "claude-opus-4-8 → claude-sonnet-4-7" in msg
        # El archivo aislado quedó con el nuevo modelo.
        written = json.loads(settings.read_text())
        assert written["model"] == "claude-sonnet-4-7"
        # Y se creó el backup .json.bak-set-model junto a él.
        backup = settings.with_suffix(".json.bak-set-model")
        assert backup.exists()
        assert json.loads(backup.read_text())["model"] == "claude-opus-4-8"

    def test_set_model_via_main(self, isolated_config, capsys):
        rc = cfg.main(["--set-model", "claude-haiku-4-5"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "modelo por defecto" in captured.out
        assert json.loads(isolated_config["settings"].read_text())["model"] == "claude-haiku-4-5"

    def test_set_model_unreadable_settings_errors(self, tmp_path, monkeypatch):
        """Settings ilegible → mensaje de error, NO escribe ni crashea."""
        ghost = tmp_path / "ghost.json"
        monkeypatch.setattr(cfg, "SETTINGS", ghost)
        msg = cfg.set_model("claude-opus-4-8")
        assert msg.startswith("ERROR:")
        assert not ghost.exists()  # no se creó nada

    def test_set_model_never_touches_real_settings(self, isolated_config):
        """Guardia dura: el ~/.claude/settings.json REAL nunca se ve afectado."""
        real = Path.home() / ".claude" / "settings.json"
        real_existed = real.exists()
        real_before = (real.stat().st_mtime, real.stat().st_size) if real_existed else None
        cfg.set_model("claude-opus-4-8")
        if real_existed:
            assert (real.stat().st_mtime, real.stat().st_size) == real_before
        else:
            assert not real.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
