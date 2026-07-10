"""Tests del inventario de capacidades (tools/capability_inventory.py).

El núcleo (parsing de frontmatter + reconciliación disco/runtime + cobertura) se
prueba con datos sintéticos para ser determinista; los escáneres se prueban en
modo smoke contra el filesystem vivo (sin acoplar a conteos exactos).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import capability_inventory as ci  # type: ignore[import-not-found]
from tools.capability_inventory import Capability  # type: ignore[import-not-found]


# --------------------------------------------------------------------------- #
# Parsing de frontmatter
# --------------------------------------------------------------------------- #
def test_read_frontmatter_inline(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: foo\ndescription: bar baz\n---\n# Cuerpo\n")
    fm = ci._read_frontmatter(f)
    assert fm["name"] == "foo"
    assert fm["description"] == "bar baz"


def test_read_frontmatter_folded_scalar(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: foo\ndescription: >\n  linea uno\n  linea dos\n---\nx\n")
    fm = ci._read_frontmatter(f)
    assert "linea uno linea dos" in ci._norm(fm["description"])


def test_read_frontmatter_none_when_absent(tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text("# sin frontmatter\n")
    assert ci._read_frontmatter(f) == {}


def test_read_frontmatter_broken_yaml_failopen(tmp_path: Path) -> None:
    f = tmp_path / "x.md"
    f.write_text("---\nname: [unclosed\n---\n")
    assert ci._read_frontmatter(f) == {}


def test_norm_collapses_whitespace() -> None:
    assert ci._norm("  a\n  b   c ") == "a b c"
    assert ci._norm(None) == ""


# --------------------------------------------------------------------------- #
# Construcción de capacidades de plugin (naming + runtime_keys)
# --------------------------------------------------------------------------- #
def test_plugin_cap_skill_naming(tmp_path: Path) -> None:
    sk = tmp_path / "my-skill" / "SKILL.md"
    sk.parent.mkdir(parents=True)
    sk.write_text("---\nname: fancy\ndescription: hace algo\n---\n")
    cap = ci._plugin_cap(sk, ns="myplug", key="myplug@mkt", scope="user", ctype="skill")
    assert cap.name == "myplug:fancy"
    assert cap.invocation == "/myplug:fancy"
    # casa por nombre de frontmatter Y por nombre de carpeta
    assert "myplug:fancy" in cap.runtime_keys()
    assert "myplug:my-skill" in cap.runtime_keys()


def test_plugin_cap_agent_invocation(tmp_path: Path) -> None:
    ag = tmp_path / "agents" / "rev.md"
    ag.parent.mkdir(parents=True)
    ag.write_text("---\nname: rev\ndescription: revisa\ntools:\n  - Read\n---\n")
    cap = ci._plugin_cap(ag, ns="p", key="p@m", scope="user", ctype="agent")
    assert cap.invocation == "Agent(subagent_type='p:rev')"
    assert cap.extra["tools"] == ["Read"]


def test_plugin_cap_agent_description_fallback(tmp_path: Path) -> None:
    # Un agente sin frontmatter YAML cae a la primera línea de prosa (no queda vacío).
    ag = tmp_path / "agents" / "noyaml.md"
    ag.parent.mkdir(parents=True)
    ag.write_text("# Post-hoc Analyzer\nHace análisis interno del proceso.\n")
    cap = ci._plugin_cap(ag, ns="p", key="p@m", scope="user", ctype="agent")
    # _first_para salta el encabezado '#' y toma la primera línea de prosa.
    assert cap.description == "Hace análisis interno del proceso."


# --------------------------------------------------------------------------- #
# Reconciliación disco <-> runtime + cobertura
# --------------------------------------------------------------------------- #
def _cap(name: str, ctype: str, keys: list[str] | None = None) -> Capability:
    extra = {"runtime_keys": keys} if keys else {}
    return Capability(name=name, ctype=ctype, invocation="x", source="user", extra=extra)


def _sample_snapshot() -> dict[str, object]:
    return {
        "captured_at": "t",
        "skills": ["a", "x:ydir", "cmd1", "builtin1"],
        "agents": ["ag1", "Explore"],
        "mcp_servers": ["m1"],
    }


def _sample_disk() -> list[Capability]:
    return [
        _cap("a", "skill"),
        _cap("x:y", "skill", keys=["x:y", "x:ydir"]),  # casa por la clave alterna
        _cap("ghost", "skill"),  # no está en runtime -> available False
        _cap("ag1", "agent"),
        _cap("cmd1", "command", keys=["cmd1"]),  # los comandos son skill-like
    ]


def test_reconcile_sets_availability() -> None:
    rec = ci.reconcile(_sample_disk(), _sample_snapshot())
    by = {c.name: c for c in rec["capabilities"]}
    assert by["a"].available is True
    assert by["x:y"].available is True  # via runtime_keys
    assert by["ghost"].available is False
    assert by["cmd1"].available is True  # comando casa contra skills del runtime
    assert by["ag1"].available is True


def test_reconcile_coverage_is_subset_full() -> None:
    cov = ci.reconcile(_sample_disk(), _sample_snapshot())["coverage"]
    assert cov["skills"] == {"runtime_total": 4, "disk_backed": 3, "runtime_only": 1}
    assert cov["agents"] == {"runtime_total": 2, "disk_backed": 1, "runtime_only": 1}
    assert cov["mcp_servers"] == {"runtime_total": 1, "disk_backed": 0, "runtime_only": 1}


def test_reconcile_generates_runtime_only() -> None:
    caps = ci.reconcile(_sample_disk(), _sample_snapshot())["capabilities"]
    runtime_only = {c.name for c in caps if c.status == "runtime"}
    assert {"builtin1", "Explore", "m1"} <= runtime_only
    assert not any(c.ctype == "builtin_tool" for c in caps)  # snapshot sin builtin_tools


def test_reconcile_no_snapshot_leaves_available_none() -> None:
    rec = ci.reconcile([_cap("a", "skill")], {})
    assert rec["capabilities"][0].available is None
    assert rec["coverage"]["has_runtime_snapshot"] is False


# --------------------------------------------------------------------------- #
# Profundidad: tools MCP por server + propósito builtin
# --------------------------------------------------------------------------- #
def test_runtime_mcp_tools_skips_aris4u() -> None:
    # aris4u se cubre en profundidad desde el source; el resto desde el snapshot.
    caps = ci._runtime_mcp_tools({"mcp_tools": {"aris4u": ["x"], "supa": ["a", "b"]}})
    assert {c.name for c in caps} == {"supa.a", "supa.b"}
    assert all(c.ctype == "mcp_tool" and c.status == "runtime" for c in caps)


def test_enrich_mcp_server_tools_attaches_list() -> None:
    cap = Capability(name="supa", ctype="mcp_server", invocation="x", source="y", description="srv")
    ci._enrich_mcp_server_tools([cap], {"mcp_tools": {"supa": ["a", "b", "c"]}})
    assert cap.extra["tools"] == ["a", "b", "c"]
    assert "3 tools" in cap.description


def test_enrich_builtin_purpose() -> None:
    cap = Capability(name="Bash", ctype="builtin_tool", invocation="Bash", source="y")
    ci._enrich_builtin_purpose([cap], {"builtin_tool_purpose": {"Bash": "Ejecutar shell"}})
    assert cap.description == "Ejecutar shell"


# --------------------------------------------------------------------------- #
# Smoke contra el filesystem vivo
# --------------------------------------------------------------------------- #
def test_scan_aris_mcp_tools_live() -> None:
    tools = ci.scan_aris_mcp_tools()
    assert tools, "debe parsear al menos una @mcp.tool() de mcp_server.py"
    assert all(t.name.startswith("aris4u.") for t in tools)
    assert all(t.ctype == "mcp_tool" for t in tools)


def test_collect_live_structure() -> None:
    data = ci.collect()
    assert "generated_at" in data
    assert "coverage" in data
    assert isinstance(data["capabilities"], list)
    assert data["capabilities"], "el inventario vivo no debe estar vacío"
    for c in data["capabilities"][:20]:
        assert c["name"] and c["ctype"] and "invocation" in c


# --------------------------------------------------------------------------- #
# Liveness (paso 2): salud verificada por capacidad
# --------------------------------------------------------------------------- #
def test_liveness_hook_broken_when_script_missing() -> None:
    cap = Capability(
        name="PreToolUse", ctype="hook", invocation="auto", source="settings.json",
        extra={"commands": ["/no/existe/script.py PreToolUse"]},
    )
    state, proof = ci._liveness_of(cap)
    assert state == "broken"
    assert "script.py" in proof


def test_liveness_disk_file_present_is_live(tmp_path: Path) -> None:
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: x\n---\n")
    cap = Capability(name="x", ctype="skill", invocation="/x", source="user", defined_at=str(f))
    assert ci._liveness_of(cap)[0] == "live"


def test_liveness_disk_file_missing_is_broken() -> None:
    cap = Capability(name="x", ctype="skill", invocation="/x", source="user", defined_at="/no/existe.md")
    assert ci._liveness_of(cap)[0] == "broken"


def test_liveness_dormant_when_not_available() -> None:
    cap = Capability(name="x", ctype="skill", invocation="/x", source="user", available=False)
    assert ci._liveness_of(cap)[0] == "dormant"


def test_liveness_runtime_only_is_external() -> None:
    cap = Capability(name="verify", ctype="skill", invocation="/verify", source="builtin", available=True)
    assert ci._liveness_of(cap)[0] == "external"


def test_live_no_broken_capabilities() -> None:
    # En un sistema sano, nada debe estar 'broken' (hook/wrapper apuntando a script ausente).
    data = ci.collect()
    assert data["coverage"]["liveness"]["broken"] == [], "hay capacidades rotas"


@pytest.mark.livehost
@pytest.mark.integration
def test_live_depth_mcp_and_hooks() -> None:
    # 100% de profundidad EXIGIBLE: cada mcp_server LOCAL (stdio, command no vacío)
    # lleva sus tools; los remotos (http/url, command vacío) solo las conocen tras
    # captura runtime — un remoto recién cableado sin tools NO es contrato roto
    # (falso rojo con stripe 2026-07-01). Los hooks llevan sus sub-handlers.
    caps = ci.collect()["capabilities"]
    servers = [c for c in caps if c["ctype"] == "mcp_server"]
    local = [c for c in servers if (c["extra"].get("command") or "").strip()]
    assert servers, "debe haber mcp_servers en el inventario"
    sin_tools = [c["name"] for c in local if not c["extra"].get("tools")]
    assert not sin_tools, f"servers LOCALES sin tools (profundidad rota): {sin_tools}"
    hooks = [c for c in caps if c["ctype"] == "hook"]
    pre = next((h for h in hooks if h["name"] == "PreToolUse"), None)
    assert pre and pre["extra"]["handlers"], "PreToolUse debe traer sus sub-handlers"
    assert "migration_linter" in pre["extra"]["blocking"]
    bts = [c for c in caps if c["ctype"] == "builtin_tool"]
    assert bts and all(c["description"] for c in bts), "todo builtin_tool debe traer propósito"


@pytest.mark.livehost
@pytest.mark.integration
def test_live_agents_are_real_not_skill_internals() -> None:
    # Regresión (audit 2026-06-22): agents/ anidados en skills/<x>/ no deben colarse
    # como agentes de plugin, y ningún agente de disco debe quedar sin descripción.
    disk_agents = [
        c for c in ci.collect()["capabilities"] if c["ctype"] == "agent" and c["status"] != "runtime"
    ]
    assert disk_agents
    for a in disk_agents:
        assert a["description"], f"agente sin descripción: {a['name']}"
        assert "/skills/" not in (a["defined_at"] or ""), f"agente interno de skill colado: {a['name']}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
