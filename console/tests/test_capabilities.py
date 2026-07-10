"""Tests del módulo de capacidades (Skills/Agents/MCP/API con audit por valor)."""
from __future__ import annotations

import pytest

from aris4u_console import capabilities as cap, live_data


def test_four_categories_available() -> None:
    """Las 4 categorías leen de fuentes reales y devuelven la estructura esperada."""
    for category in cap.CATEGORIES:
        d = cap.read_capability(category)
        assert d["available"] is True, f"{category} no disponible: {d.get('reason')}"
        assert d["category"] == category
        assert isinstance(d["groups"], list)  # puede estar vacío en un install fresco (CI)
        assert d["summary"]["total"] >= 0


def test_groups_ordered_claude_first() -> None:
    """El orden de grupos es siempre Claude antes que ARIS4U."""
    for category in cap.CATEGORIES:
        srcs = [g["source"] for g in cap.read_capability(category)["groups"]]
        if "claude" in srcs and "aris4u" in srcs:
            assert srcs.index("claude") < srcs.index("aris4u")


def test_items_have_audit_fields() -> None:
    """Cada item trae el audit resuelto: estado, uso, veredicto válido."""
    valid_verdicts = set(cap._VERDICTS)
    for category in cap.CATEGORIES:
        for g in cap.read_capability(category)["groups"]:
            for it in g["items"]:
                assert it["estado"] in {"activo", "inactivo"}
                assert it["verdict"] in valid_verdicts
                assert it["verdict_label"] == cap._VERDICTS[it["verdict"]]
                assert it["uso"] is None or isinstance(it["uso"], int)


def test_verdict_logic() -> None:
    """El veredicto deriva correctamente de estado/uso/redundancia."""
    assert cap._verdict(activo=False, uso=99, redundante=False) == "inactivo"
    assert cap._verdict(activo=True, uso=0, redundante=False) == "ocioso"
    assert cap._verdict(activo=True, uso=5, redundante=False) == "promover"
    assert cap._verdict(activo=True, uso=50, redundante=False) == "usar"
    assert cap._verdict(activo=True, uso=99, redundante=True) == "revisar"
    assert cap._verdict(activo=True, uso=None, redundante=False) == "activo"


def test_agents_have_real_usage() -> None:
    """Los agents traen uso real de la telemetría (no None) y no se duplican."""
    d = cap.read_capability("agents")
    items = [it for g in d["groups"] for it in g["items"]]
    assert all(isinstance(it["uso"], int) for it in items)
    keys = [(it["name"], it["source"]) for it in items]
    assert len(keys) == len(set(keys)), "hay agents duplicados (falta dedup)"


def test_unknown_category_fails_soft() -> None:
    d = cap.read_capability("inexistente")
    assert d["available"] is False and "reason" in d


def test_uso_label_buckets() -> None:
    assert cap._uso_label(None) == "sin datos"
    assert cap._uso_label(0) == "nulo"
    assert cap._uso_label(5) == "bajo"
    assert cap._uso_label(25) == "medio"
    assert cap._uso_label(100) == "alto"


def test_items_have_last_used_and_cuando() -> None:
    """Cada item trae última fecha de uso (str) y el campo 'cuando'."""
    for category in cap.CATEGORIES:
        for g in cap.read_capability(category)["groups"]:
            for it in g["items"]:
                assert isinstance(it["last_used"], str)
                assert it["last_used_label"] == "nunca" or len(it["last_used_label"]) == 10
                assert isinstance(it["cuando"], str)


def test_health_runs_for_all_categories() -> None:
    """El smoke test corre para las 4 categorías y devuelve resultados con ok booleano."""
    for category in cap.CATEGORIES:
        h = cap.health(category)
        assert h["available"] is True
        assert h["summary"]["total"] >= 0
        assert h["summary"]["ok"] + h["summary"]["fail"] == h["summary"]["total"]
        for r in h["results"]:
            assert isinstance(r["ok"], bool)
            assert r["name"] and r["detail"]


def test_frontmatter_handles_block_scalar(tmp_path) -> None:
    """El parser de frontmatter maneja descripciones YAML multilínea (description: |)."""
    md = tmp_path / "x.md"
    md.write_text("---\nname: foo\ndescription: |\n  Primera línea.\n  Segunda línea.\n---\nbody",
                  encoding="utf-8")
    fm = cap._frontmatter(md)
    assert fm["name"] == "foo"
    assert "Primera línea." in fm["description"] and "Segunda" in fm["description"]


def test_health_unknown_category_fails_soft() -> None:
    assert cap.health("inexistente")["available"] is False


# --- Casos límite con data SINTÉTICA (fail-soft real, no solo el happy path) ---

def test_version_key_semantic_order() -> None:
    """El fix del bug: ordena versiones por semántica, no lexicográfico; semver gana a hash."""
    vk = cap._version_key
    assert vk("2.2.60") > vk("2.2.9")      # 60 > 9 (lexicográfico daría lo contrario)
    assert vk("2.2.60") > vk("2.2.49")
    assert vk("2.2.0") > vk("c7c92a10")    # semver gana a un hash de dev
    assert vk("10.0.0") > vk("9.9.9")

@pytest.mark.parametrize("content,name_ok,desc_ok", [
    ("---\nname: x\ndescription: hola\n---\nbody", True, True),          # válido
    ("---\nname: x\ndescription: |\n  multi\n  línea\n---\n", True, True),  # block scalar
    ("sin frontmatter alguno", False, False),                            # roto
    ("---\nname: x\n---\nbody", True, False),                            # falta description
    ("", False, False),                                                  # vacío
])
def test_frontmatter_robusto(tmp_path, content, name_ok, desc_ok) -> None:
    """El parser de frontmatter no crashea con archivos rotos/vacíos/multilínea."""
    md = tmp_path / "s.md"
    md.write_text(content, encoding="utf-8")
    fm = cap._frontmatter(md)
    assert bool(fm.get("name")) == name_ok
    assert bool(fm.get("description")) == desc_ok

def test_usage_missing_log_is_empty(tmp_path) -> None:
    """Si el log de telemetría no existe, usage() devuelve counters vacíos (no crash)."""
    u = cap.usage(repo=tmp_path)
    assert u["window"] == 0
    assert all(len(u[k]) == 0 for k in ("mcp_server", "agents", "hooks"))
    assert u["last"] == {"mcp_server": {}, "agents": {}, "hooks": {}}

def test_health_md_flags_broken_file(tmp_path) -> None:
    """El smoke test marca ❌ un archivo inexistente o con frontmatter incompleto."""
    from pathlib import Path
    ok = tmp_path / "ok" / "SKILL.md"
    ok.parent.mkdir()
    ok.write_text("---\nname: ok\ndescription: d\n---\n", encoding="utf-8")
    bad = tmp_path / "bad" / "SKILL.md"
    bad.parent.mkdir()
    bad.write_text("roto", encoding="utf-8")
    res = cap._health_md([("ok", "", ok, ""), ("bad", "", bad, ""),
                          ("ausente", "", Path("/no/existe.md"), "")])
    by = {r["name"]: r["ok"] for r in res}
    assert by["ok"] is True and by["bad"] is False and by["ausente"] is False

def test_translate_falls_back_without_ollama() -> None:
    """Sin llamar a Ollama (allow_call=False), un texto inglés se devuelve igual (fail-soft)."""
    from aris4u_console import translate
    out = translate.translate("Designs feature architectures", cache={}, allow_call=False)
    assert out == "Designs feature architectures"   # no rompe, devuelve original
    assert translate.translate("ya está en español con ñ", cache={}) == "ya está en español con ñ"

def test_grouped_translates_only_non_spanish() -> None:
    """_grouped no rompe si el caché está vacío y deja el texto si no hay traducción."""
    items = [cap._item("x", "English text here", "claude", activo=True, uso=0,
                       redundante=False, where="~")]
    d = cap._grouped(items, "skills")
    assert d["available"] and d["groups"][0]["items"][0]["desc"]  # no vacío, no crash


# ---------------------------------------------------------------------------
# Bug-fix: MCP remote connectors (empty command) → uso=None, not uso=0
# ---------------------------------------------------------------------------

def test_mcp_remote_connector_uso_is_none(tmp_path, monkeypatch) -> None:
    """Servidores MCP sin command (HTTP/OAuth) → uso=None ('sin datos'), verdict='activo'.

    Antes del fix, stripe y cloudflare-builds (command='', url=...) recibían uso=0
    (forzado) → verdict='ocioso', lo que era engañoso: sus llamadas van al endpoint
    remoto y nunca aparecen en la telemetría local.
    """
    import json

    fake_cfg = tmp_path / ".claude.json"
    fake_cfg.write_text(json.dumps({
        "mcpServers": {
            "my-stdio":  {"command": "npx", "args": ["-y", "some-mcp"]},
            "my-remote": {"command": "", "url": "https://example.mcp/mcp"},
        }
    }), encoding="utf-8")

    # Log vacío → sin llamadas registradas para ningún server
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "v16.1-events.jsonl").write_text("", encoding="utf-8")

    original_home = cap.HOME
    monkeypatch.setattr(cap, "HOME", tmp_path)
    try:
        d = cap.read_mcp(repo=tmp_path)
    finally:
        monkeypatch.setattr(cap, "HOME", original_home)

    all_items = {it["name"]: it for g in d["groups"] for it in g["items"]}

    # Server stdio sin uso real → ocioso (correcto: sabemos que no se llamó)
    assert "my-stdio" in all_items
    assert all_items["my-stdio"]["uso"] == 0
    assert all_items["my-stdio"]["verdict"] == "ocioso"
    assert all_items["my-stdio"]["kind"] == "mcp-stdio"

    # Server remoto HTTP → sin datos locales (uso=None, verdict='activo')
    assert "my-remote" in all_items
    assert all_items["my-remote"]["uso"] is None, (
        "conector HTTP sin command debe tener uso=None, no uso=0"
    )
    assert all_items["my-remote"]["verdict"] == "activo", (
        "conector HTTP sin telemetria local debe ser 'activo' (neutral), no 'ocioso'"
    )
    assert all_items["my-remote"]["kind"] == "mcp-remote"

    # El summary debe reflejar al menos un 'activo' (el remoto)
    summary = d["summary"]
    assert summary["activo"] >= 1, "summary.activo debe ser >=1 cuando hay conectores remotos"
    assert summary["ocioso"] >= 1, "summary.ocioso debe seguir contando los stdio sin uso"


def test_mcp_real_usage_crossed_with_telemetry(tmp_path, monkeypatch) -> None:
    """Servers stdio con llamadas reales en telemetria reciben el veredicto correcto.

    Verifica el cruce telemetria->veredicto: uso alto -> 'usar', uso bajo -> 'promover'.
    """
    import json
    events = (
        [json.dumps({"event": "mcp_call", "server": "my-server", "tool": "do_thing",
                     "ts": "2026-06-01T12:00:00"})] * 12
        + [json.dumps({"event": "mcp_call", "server": "my-low-use", "tool": "x",
                       "ts": "2026-06-01T12:00:02"})]
    )
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "v16.1-events.jsonl").write_text("\n".join(events), encoding="utf-8")

    fake_cfg = tmp_path / ".claude.json"
    fake_cfg.write_text(json.dumps({
        "mcpServers": {
            "my-server":   {"command": "npx", "args": []},
            "my-low-use":  {"command": "npx", "args": []},
            "my-never":    {"command": "npx", "args": []},
        }
    }), encoding="utf-8")

    original_home = cap.HOME
    monkeypatch.setattr(cap, "HOME", tmp_path)
    try:
        d = cap.read_mcp(repo=tmp_path)
    finally:
        monkeypatch.setattr(cap, "HOME", original_home)

    by_name = {it["name"]: it for g in d["groups"] for it in g["items"]}

    assert by_name["my-server"]["uso"] == 12
    assert by_name["my-server"]["verdict"] == "usar"

    assert by_name["my-low-use"]["uso"] == 1
    assert by_name["my-low-use"]["verdict"] == "promover"

    assert by_name["my-never"]["uso"] == 0
    assert by_name["my-never"]["verdict"] == "ocioso"


# ---- FIX 5: _local_mcp_servers delega a live_data._read_global_claude_servers ----

def test_local_mcp_servers_delegates_to_live_data(monkeypatch, tmp_path) -> None:
    """FIX 5: _local_mcp_servers() llama a live_data._read_global_claude_servers (un solo lector).

    Garantiza que /cap/mcp y /config leen desde el mismo origen: si la ruta de
    ~/.claude.json cambia, solo hay que tocar live_data._read_global_claude_servers.
    """
    called_with: list = []

    def fake_read(home):  # type: ignore[override]
        called_with.append(home)
        return {"fake-server": {"command": "fake-bin"}}

    monkeypatch.setattr(live_data, "_read_global_claude_servers", fake_read)
    monkeypatch.setattr(cap, "HOME", tmp_path)
    result = cap._local_mcp_servers()
    assert called_with, "_local_mcp_servers no llamó a live_data._read_global_claude_servers"
    assert called_with[0] == tmp_path, "se llamó con el HOME incorrecto"
    assert "fake-server" in result, "resultado no refleja lo que devolvió _read_global_claude_servers"


# ---- FIX 2: read_mcp incluye MCP de plugins (no solo ~/.claude.json) ----

def test_read_mcp_includes_plugin_servers(monkeypatch, tmp_path) -> None:
    """FIX 2: read_mcp() incluye los MCP aportados por plugins instalados.

    Antes solo leía ~/.claude.json (6 globales). Ahora también recorre el cache de
    plugins (figma/firebase/serena/shadcn/etc.) via live_data._mcp_from_plugin_cache.
    """
    import json as _json

    # HOME sintético con ~/.claude.json vacío (sin MCPs globales)
    (tmp_path / ".claude.json").write_text(
        _json.dumps({"mcpServers": {}}), encoding="utf-8")
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Plugin cache con un MCP server de figma
    plugin_path = claude_dir / "plugins" / "cache" / "figma" / "1.0.0"
    plugin_path.mkdir(parents=True)
    (plugin_path / ".mcp.json").write_text(
        _json.dumps({"mcpServers": {"claude_ai_Figma": {"command": "figma-mcp"}}}),
        encoding="utf-8")
    installed = {"plugins": {"figma@official": [{"installPath": str(plugin_path)}]}}
    (claude_dir / "plugins").mkdir(exist_ok=True)
    (claude_dir / "plugins" / "installed_plugins.json").write_text(
        _json.dumps(installed), encoding="utf-8")

    # Log vacío — sin telemetría para este server (ocioso, honesto)
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "logs" / "v16.1-events.jsonl").write_text("", encoding="utf-8")

    monkeypatch.setattr(cap, "HOME", tmp_path)
    d = cap.read_mcp(repo=tmp_path)

    all_names = {it["name"] for g in d["groups"] for it in g["items"]}
    assert "claude_ai_Figma" in all_names, (
        f"read_mcp no incluyó el server de plugin 'claude_ai_Figma'. "
        f"Servers encontrados: {sorted(all_names)}"
    )
