"""Tests del generador de inventario vivo (aris4u_console/inventory.py).

Verifica el auto-descubrimiento por familia, la detección de cobertura de tests, las MCP
tools y la madurez derivada, sobre un repo sintético en tmp (sin depender de git).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aris4u_console import inventory  # noqa: E402


def _make_repo(tmp: Path) -> Path:
    """Crea un repo ARIS4U sintético mínimo en tmp."""
    (tmp / "hooks/dispatch/events").mkdir(parents=True)
    (tmp / "hooks/dispatch/events/user_prompt_submit.py").write_text(
        '"""Hook del prompt del usuario."""\n', encoding="utf-8")
    (tmp / "hooks/dispatch/handlers").mkdir(parents=True)
    (tmp / "hooks/dispatch/handlers/phi_guard.py").write_text(
        '"""Guard de PHI."""\n', encoding="utf-8")
    (tmp / "tools").mkdir()
    (tmp / "tools/freeze_report.py").write_text('"""Reporte de freeze."""\n', encoding="utf-8")
    (tmp / "engine/v16").mkdir(parents=True)
    (tmp / "engine/v16/model_router.py").write_text('"""Router."""\n', encoding="utf-8")
    (tmp / "integrations").mkdir()
    (tmp / "integrations/mcp_server.py").write_text(
        "@mcp.tool()\ndef aris_health():\n    pass\n\n@mcp.tool()\ndef aris_search():\n    pass\n",
        encoding="utf-8")
    (tmp / "data").mkdir()
    (tmp / "data/sessions.db").write_bytes(b"x" * 10)
    (tmp / "tests").mkdir()
    (tmp / "tests/test_freeze_report.py").write_text("# cubre freeze_report\n", encoding="utf-8")
    return tmp


def test_discovers_all_families(tmp_path: Path) -> None:
    """Cada familia se descubre con el conteo correcto."""
    repo = _make_repo(tmp_path)
    inv = inventory.build_inventory(repo, external_home=repo)
    fams = inv["by_family"]
    assert fams["hook_event"] == 1
    assert fams["hook_handler"] == 1
    assert fams["tool"] == 1
    assert fams["engine"] == 1
    assert fams["mcp_tool"] == 2
    assert fams["integration"] == 1  # mcp_server.py también es un componente (además de sus tools)
    assert fams["database"] == 1
    assert inv["totals"]["components"] == 8


def test_test_coverage_detected(tmp_path: Path) -> None:
    """freeze_report tiene test_freeze_report → has_test True; los demás False."""
    repo = _make_repo(tmp_path)
    inv = inventory.build_inventory(repo, external_home=repo)
    fr = next(c for c in inv["components"] if c["name"] == "freeze_report")
    pg = next(c for c in inv["components"] if c["name"] == "phi_guard")
    assert fr["signals"]["has_test"] is True
    assert pg["signals"]["has_test"] is False


def test_mcp_tools_parsed(tmp_path: Path) -> None:
    """Las MCP tools se extraen del decorador @mcp.tool()."""
    repo = _make_repo(tmp_path)
    inv = inventory.build_inventory(repo, external_home=repo)
    names = {c["name"] for c in inv["components"] if c["family"] == "mcp_tool"}
    assert names == {"aris_health", "aris_search"}


def test_dead_hook_scripts_marked_muerto(tmp_path: Path) -> None:
    """Los .sh en hooks/ portados (no los 3 vivos) se marcan 'muerto'."""
    (tmp_path / "hooks").mkdir()
    (tmp_path / "hooks/depth_inject.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (tmp_path / "hooks/nightly_vacuum.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    comps = {c.name: c for c in inventory.discover_scripts(tmp_path)}
    assert comps["depth_inject.sh"].maturity == "muerto"
    assert comps["nightly_vacuum.sh"].maturity == "vivo"


def test_external_discovery(tmp_path: Path) -> None:
    """discover_external encuentra claude-mem.db + settings.json bajo el HOME dado."""
    (tmp_path / ".claude-mem").mkdir()
    (tmp_path / ".claude-mem/claude-mem.db").write_bytes(b"x" * 20)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/settings.json").write_text("{}", encoding="utf-8")
    names = {c.name for c in inventory.discover_external(tmp_path)}
    assert names == {"claude-mem.db", "settings.json"}


def test_maturity_derivation() -> None:
    """Madurez derivada de señales (no etiqueta a mano)."""
    assert inventory._derive_maturity(False, "2026-06-19") == "sin_test"
    assert inventory._derive_maturity(True, "") == "estable"
    assert inventory._derive_maturity(True, "1999-01-01") == "estable"
    assert inventory._derive_maturity(True, date_today_str()) == "vivo"


def date_today_str() -> str:
    """Fecha de hoy YYYY-MM-DD (para el caso 'vivo')."""
    from datetime import date
    return date.today().isoformat()
