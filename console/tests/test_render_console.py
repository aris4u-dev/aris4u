"""Tests del casador vivo↔curado (aris4u_console/render_console.py).

El casamiento débil era la raíz de varios errores (drift falso, piezas sin estado vivo).
Estas pruebas fijan: casar por ruta completa (dir-alineado), desambiguar basenames repetidos,
casar por token exacto, y NO casar nombres no-código (.sh/.db/.json).
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aris4u_console import render_console as rc  # noqa: E402

_LIVE = {
    "components": [
        {"name": "user_prompt_submit", "path": "hooks/dispatch/events/user_prompt_submit.py"},
        {"name": "migration_linter", "path": "hooks/dispatch/handlers/migration_linter.py"},
        {"name": "migration_linter", "path": "tools/migration_linter.py"},  # basename repetido
        {"name": "queueing", "path": "engine/v16/orchestration/queueing.py"},
        {"name": "phi_sanitizer", "path": "hooks/dispatch/handlers/phi_sanitizer.py"},
        {"name": "mcp_server", "path": "integrations/mcp_server.py"},
    ]
}


def _idx() -> dict:
    return rc._live_index(_LIVE)


def test_path_match_dir_aligned() -> None:
    """'events/user_prompt_submit.py' casa la ruta viva que termina en ese sufijo."""
    m = rc.match_live({"name": "events/user_prompt_submit.py — hook del prompt"}, _idx())
    assert m and m["path"] == "hooks/dispatch/events/user_prompt_submit.py"


def test_path_match_disambiguates_duplicate_basename() -> None:
    """Con dos migration_linter.py, la ruta curada con dir gana la correcta."""
    idx = _idx()
    h = rc.match_live({"name": "handlers/migration_linter.py (BLOQUEANTE)"}, idx)
    t = rc.match_live({"name": "tools/migration_linter.py"}, idx)
    assert h["path"] == "hooks/dispatch/handlers/migration_linter.py"  # type: ignore[index]  # match_live returns dict|None; test asserts match succeeds
    assert t["path"] == "tools/migration_linter.py"  # type: ignore[index]  # match_live returns dict|None; test asserts match succeeds


def test_bare_filename_matches_by_basename() -> None:
    """'queueing.py — teoría de colas' casa por basename único."""
    m = rc.match_live({"name": "queueing.py — teoría de colas"}, _idx())
    assert m and m["path"].endswith("orchestration/queueing.py")


def test_token_match_when_no_py_path() -> None:
    """Un nombre compuesto sin .py casa por identificador exacto (phi_sanitizer)."""
    m = rc.match_live({"name": "handlers/phi_sanitizer / f5_prevalidation / schema_drift"}, _idx())
    assert m and m["name"] == "phi_sanitizer"


def test_non_code_names_do_not_match() -> None:
    """Nombres no-código (.sh/.db/.json) NO deben casar (quedan curado-only)."""
    idx = _idx()
    assert rc.match_live({"name": "mlx_serve.sh"}, idx) is None
    assert rc.match_live({"name": "claude-mem.db — memoria externa"}, idx) is None
    assert rc.match_live({"name": ".sh MUERTOS + orchestrator_enforcer.sh (INERTE)"}, idx) is None


def test_scope_badge_for_medical() -> None:
    """El badge médico aparece solo en piezas PHI/clínicas."""
    assert "vertical médico" in rc._scope_badge({"name": "phi_guard", "what_for": "guard"})
    assert rc._scope_badge({"name": "queueing", "what_for": "teoría de colas"}) == ""
