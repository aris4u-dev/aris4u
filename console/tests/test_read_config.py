"""read_config: la consola surfacea la config efectiva de ARIS4U.

Fuentes:
- model_default / env / settings_path → tools/aris_config.collect() (sin cambio).
- MCP cableados → _discover_mcps(): lee ~/.claude.json (global VIVO), plugin cache,
  local plugins y .mcp.json del repo. No lee settings.json (que tiene 0 mcpServers).
"""
from __future__ import annotations

import json
from pathlib import Path

from aris4u_console import live_data


def _make_repo_with_config(tmp: Path, collect_returns: dict) -> Path:
    """Crea un repo sintético con un tools/aris_config.py cuyo collect() devuelve lo dado."""
    (tmp / "tools").mkdir(parents=True)
    body = (
        "def collect():\n"
        f"    return {collect_returns!r}\n"
    )
    (tmp / "tools" / "aris_config.py").write_text(body, encoding="utf-8")
    return tmp


def _make_fake_home(tmp: Path, mcpServers: dict | None = None) -> Path:
    """Crea un HOME sintético con ~/.claude.json (archivo VIVO) para aislar del entorno real.

    El descubrimiento global lee ``home/.claude.json`` (HOME), no ``home/.claude/.claude.json``
    (ese era stale, 16-abr). También crea el dir ``.claude`` vacío por si otros lectores lo tocan.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / ".claude").mkdir(parents=True, exist_ok=True)
    (tmp / ".claude.json").write_text(
        json.dumps({"mcpServers": mcpServers or {}}), encoding="utf-8")
    return tmp


# --- Tests de read_config (contrato + fail-soft) ---

def test_read_config_reuses_collect_model_and_env(tmp_path: Path) -> None:
    """model_default y env siguen viniendo de aris_config.collect()."""
    repo = _make_repo_with_config(tmp_path / "repo", {
        "model_default": "opus[1m]", "env": {"ARIS4U_HEALTHCARE": "0"},
        "settings_path": "/x/settings.json",
    })
    out = live_data.read_config(repo)
    assert out["available"] is True
    assert out["model_default"] == "opus[1m]"
    assert out["env"] == {"ARIS4U_HEALTHCARE": "0"}
    # MCP ya no viene de collect(); ahora lo provee _discover_mcps
    assert isinstance(out["mcp_global"], list)
    assert isinstance(out["mcp_repo"], list)
    assert isinstance(out["mcp_duplicated"], list)


def test_read_config_missing_file_is_failsoft(tmp_path: Path) -> None:
    out = live_data.read_config(tmp_path)  # sin tools/aris_config.py
    assert out["available"] is False
    assert "no se encontró" in out["reason"]


def test_read_config_collect_error_is_failsoft(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "aris_config.py").write_text(
        "def collect():\n    raise RuntimeError('boom')\n", encoding="utf-8")
    out = live_data.read_config(tmp_path)
    assert out["available"] is False
    assert "boom" in out["reason"]


# --- Tests de _discover_mcps (nueva lógica de descubrimiento) ---

def test_discover_mcps_reads_dot_claude_json(tmp_path: Path) -> None:
    """_discover_mcps lee ~/.claude.json (HOME, vivo) y reporta MCPs como 'global'."""
    home = _make_fake_home(tmp_path / "home", {
        "myserver": {"type": "stdio", "command": "myserver-bin", "args": []},
    })
    result = live_data._discover_mcps(home=home, repo=None)
    assert "myserver" in result["mcp_global"]
    by_name = {e["name"]: e for e in result["mcp_by_source"]}
    assert by_name["myserver"]["origin"] == "global"
    assert by_name["myserver"]["command"] == "myserver-bin"


def test_discover_mcps_reads_repo_mcp_json(tmp_path: Path) -> None:
    """_discover_mcps lee .mcp.json del repo y lo pone en mcp_repo."""
    home = _make_fake_home(tmp_path / "home")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"aris4u": {"command": "mcp_wrapper.sh"}}}),
        encoding="utf-8")
    result = live_data._discover_mcps(home=home, repo=repo)
    assert "aris4u" in result["mcp_repo"]


def test_discover_mcps_detects_duplicates(tmp_path: Path) -> None:
    """Un MCP presente en global Y repo aparece en mcp_duplicated."""
    home = _make_fake_home(tmp_path / "home", {
        "shared": {"command": "shared-bin"},
    })
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"shared": {"command": "shared-bin"}}}),
        encoding="utf-8")
    result = live_data._discover_mcps(home=home, repo=repo)
    assert "shared" in result["mcp_duplicated"]


def test_discover_mcps_reads_plugin_cache(tmp_path: Path) -> None:
    """_discover_mcps lee .mcp.json del cache de plugins instalados."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    # Crear estructura de plugin cache
    plugin_path = claude_dir / "plugins" / "cache" / "myplugin" / "1.0.0"
    plugin_path.mkdir(parents=True)
    (plugin_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"myplugin-mcp": {"command": "npx", "args": ["myplugin"]}}}),
        encoding="utf-8")
    # installed_plugins.json referencia la ruta de instalación
    installed = {"plugins": {"myplugin@official": [{"installPath": str(plugin_path)}]}}
    (claude_dir / "plugins").mkdir(exist_ok=True)
    (claude_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps(installed), encoding="utf-8")
    result = live_data._discover_mcps(home=home, repo=None)
    assert "myplugin-mcp" in result["mcp_global"]
    by_name = {e["name"]: e for e in result["mcp_by_source"]}
    assert by_name["myplugin-mcp"]["origin"] == "plugin:myplugin"


def test_discover_mcps_empty_home_is_failsoft(tmp_path: Path) -> None:
    """_discover_mcps con HOME vacío devuelve listas vacías sin crashear."""
    home = tmp_path / "empty-home"
    home.mkdir()
    result = live_data._discover_mcps(home=home, repo=None)
    assert result["mcp_global"] == []
    assert result["mcp_repo"] == []
    assert result["mcp_duplicated"] == []
    assert result["mcp_by_source"] == []


def test_discover_mcps_mcp_by_source_present(tmp_path: Path) -> None:
    """mcp_by_source siempre está presente (lista, puede ser vacía)."""
    home = _make_fake_home(tmp_path / "home")
    result = live_data._discover_mcps(home=home, repo=None)
    assert isinstance(result["mcp_by_source"], list)


def test_read_config_exposes_mcp_by_source(tmp_path: Path) -> None:
    """read_config expone mcp_by_source (clave nueva para detalle de origen)."""
    repo = _make_repo_with_config(tmp_path / "repo", {
        "model_default": "opus[1m]", "env": {}, "settings_path": "/x",
    })
    out = live_data.read_config(repo)
    assert out["available"] is True
    assert "mcp_by_source" in out
    assert isinstance(out["mcp_by_source"], list)
