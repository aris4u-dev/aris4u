"""Cobertura de hooks/dispatch/handlers/code_quality_gate.py.

Handler PostToolUse Write|Edit|MultiEdit: corre ruff + radon sobre el .py tocado,
emite un additionalContext advisory anti-degradación y registra el resultado en la
tabla `gate_results` (telemetría). Advisory puro, fail-open total.

Estrategia de aislamiento (las fixtures autouse de conftest NO cubren este handler:
`_record` escribe en `ARIS4U_ROOT/data/sessions.db` por sqlite3 DIRECTO, no por
session_manager). Por eso:
  - `_isolated_root` (autouse local) re-apunta `code_quality_gate.ARIS4U_ROOT` a un
    tmp con su propia tabla `gate_results` → NUNCA toca el DB real.
  - `subprocess.run` se MOCKEA siempre (no se corre ruff/radon de verdad).
  - El fail-open de binario ausente se ejercita parcheando `_RUFF`/`_RADON` a rutas
    inexistentes.

Importa el módulo directo (patrón tests/engine), no vía _invoke: este handler expone
`run()` puro que devuelve string, sin stdin/JSON.

Corre:  .venv312/bin/python3 -m pytest tests/dispatch/test_code_quality_gate.py -q
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from collections.abc import Callable
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from dispatch.handlers import code_quality_gate as g  # noqa: E402

# --------------------------------------------------------------------------- #
# Fixtures de aislamiento
# --------------------------------------------------------------------------- #


def _make_sessions_db(db_path: Path) -> None:
    """Crea un sessions.db tmp con la tabla gate_results (mismo schema que el real)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("""
            CREATE TABLE gate_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module_name TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                details TEXT,
                e2e_prompt TEXT,
                session_ref TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _isolated_root(tmp_path, monkeypatch) -> Path:
    """Re-apunta ARIS4U_ROOT del handler a un tmp con data/sessions.db aislado.

    SAGRADO: garantiza que `_record` jamás escriba en el data/sessions.db real.
    Devuelve la ruta del DB tmp para que los tests inspeccionen las filas.
    """
    fake_root = tmp_path / "aris_root"
    (fake_root / "data").mkdir(parents=True)
    db = fake_root / "data" / "sessions.db"
    _make_sessions_db(db)
    monkeypatch.setattr(g, "ARIS4U_ROOT", fake_root)
    return db


def _gate_rows(db: Path) -> list[dict[str, Any]]:
    """Lee todas las filas de gate_results del DB tmp como dicts."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM gate_results")]
    finally:
        conn.close()


def _proc(stdout: str = "", returncode: int = 0) -> mock.MagicMock:
    """Fabrica un resultado de subprocess.run mockeado."""
    return mock.MagicMock(stdout=stdout, stderr="", returncode=returncode)


# --------------------------------------------------------------------------- #
# Gating: solo Write/Edit/MultiEdit sobre .py reales
# --------------------------------------------------------------------------- #


def test_non_write_edit_tool_is_noop(tmp_path) -> None:
    """Bash/Read/Agent → no-op, devuelve "" y no toca subprocess."""
    py = tmp_path / "x.py"
    py.write_text("x = 1\n")
    with mock.patch.object(g.subprocess, "run") as run:
        assert g.run("Bash", {"file_path": str(py)}) == ""
        assert g.run("Read", {"file_path": str(py)}) == ""
    run.assert_not_called()


def test_non_py_file_is_noop(tmp_path) -> None:
    """Un Write a un .txt → no aplica (solo .py)."""
    txt = tmp_path / "notes.txt"
    txt.write_text("hola")
    with mock.patch.object(g.subprocess, "run") as run:
        assert g.run("Write", {"file_path": str(txt)}) == ""
    run.assert_not_called()


def test_nonexistent_py_is_noop(tmp_path) -> None:
    """file_path .py que no existe en disco → no-op (no es archivo real)."""
    ghost = tmp_path / "ghost.py"  # nunca creado
    with mock.patch.object(g.subprocess, "run") as run:
        assert g.run("Edit", {"file_path": str(ghost)}) == ""
    run.assert_not_called()


def test_dependency_path_is_skipped(tmp_path) -> None:
    """Archivos bajo .venv/site-packages/etc no se chequean (marcadores SKIP)."""
    vdir = tmp_path / "proj" / ".venv" / "lib"
    vdir.mkdir(parents=True)
    dep = vdir / "dep.py"
    dep.write_text("x = 1\n")
    with mock.patch.object(g.subprocess, "run") as run:
        assert g.run("Write", {"file_path": str(dep)}) == ""
    run.assert_not_called()


def test_missing_file_path_is_noop() -> None:
    """tool_input sin file_path (o None) → "" sin reventar."""
    assert g.run("Write", {}) == ""
    assert g.run("Write", {"file_path": ""}) == ""
    assert g.run("Write", None) == ""


# --------------------------------------------------------------------------- #
# Caso limpio: sin issues ni hotspots → "" + registro status="clean"
# --------------------------------------------------------------------------- #


def test_clean_file_returns_empty_and_records_clean(tmp_path, _isolated_root) -> None:
    py = tmp_path / "clean.py"
    py.write_text("x = 1\n")

    # ruff sin salida (limpio), radon sin bloques.
    with mock.patch.object(g.subprocess, "run", return_value=_proc(stdout="")) as run:
        out = g.run("Write", {"file_path": str(py)})

    assert out == "", "código limpio no debe emitir advisory"
    assert run.call_count == 2, "debe correr ruff Y radon"

    rows = _gate_rows(_isolated_root)
    assert len(rows) == 1
    assert rows[0]["status"] == "clean"
    assert rows[0]["module_name"] == "clean.py"
    details = json.loads(rows[0]["details"])
    assert details == {"lint": 0, "hotspots": 0}


# --------------------------------------------------------------------------- #
# Caso con issues de ruff: formato advisory + registro status="issues"
# --------------------------------------------------------------------------- #


def _ruff_then_radon(ruff_stdout: str, radon_stdout: str) -> Callable[..., Any]:
    """side_effect que devuelve ruff en la 1ª llamada y radon en la 2ª."""
    seq = iter([_proc(stdout=ruff_stdout), _proc(stdout=radon_stdout)])

    def _side(*_a: Any, **_k: Any) -> Any:
        return next(seq)

    return _side


def test_ruff_issues_formatted_and_recorded(tmp_path, _isolated_root) -> None:
    ruff_out = (
        f"{tmp_path}/bad.py:1:1: F401 `os` imported but unused\n"
        f"{tmp_path}/bad.py:2:5: E225 missing whitespace around operator\n"
    )
    py = tmp_path / "bad.py"
    py.write_text("import os\nx=1\n")

    with mock.patch.object(g.subprocess, "run", side_effect=_ruff_then_radon(ruff_out, "{}")):
        out = g.run("Edit", {"file_path": str(py)})

    assert "Code quality gate — bad.py" in out
    assert "ruff: 2 issue(s)" in out
    # El path se recorta: queda el código + mensaje, no la ruta absoluta.
    assert "F401 `os` imported but unused" in out
    assert str(tmp_path) not in out, "el path absoluto debe recortarse del advisory"

    rows = _gate_rows(_isolated_root)
    assert len(rows) == 1
    assert rows[0]["status"] == "issues"
    details = json.loads(rows[0]["details"])
    assert details["lint"] == 2
    assert details["hotspots"] == 0


def test_ruff_truncates_to_five_with_overflow_marker(tmp_path, _isolated_root) -> None:
    """Más de 5 issues → muestra 5 y un marcador '… +N más'."""
    lines = "".join(f"{tmp_path}/m.py:{i}:1: F401 unused {i}\n" for i in range(1, 9))
    py = tmp_path / "m.py"
    py.write_text("x = 1\n")

    with mock.patch.object(g.subprocess, "run", side_effect=_ruff_then_radon(lines, "{}")):
        out = g.run("Write", {"file_path": str(py)})

    assert "ruff: 8 issue(s)" in out
    assert "… +3 más" in out  # 8 - 5 mostrados
    rows = _gate_rows(_isolated_root)
    assert json.loads(rows[0]["details"])["lint"] == 8


# --------------------------------------------------------------------------- #
# Caso con hotspots de radon (complejidad ≥ umbral)
# --------------------------------------------------------------------------- #


def test_radon_hotspots_above_threshold_reported(tmp_path, _isolated_root) -> None:
    """Funciones con CC ≥ umbral se reportan; las < umbral se ignoran."""
    thr = g._COMPLEXITY_THRESHOLD
    hi, mid, low = thr + 10, thr + 2, thr - 7  # dos sobre umbral, una bajo
    radon_json = json.dumps(
        {
            str(tmp_path / "h.py"): [
                {"name": "monster", "complexity": hi},
                {"name": "ok_fn", "complexity": low},  # bajo umbral → ignorado
                {"name": "spaghetti", "complexity": mid},
            ]
        }
    )
    py = tmp_path / "h.py"
    py.write_text("x = 1\n")

    with mock.patch.object(g.subprocess, "run", side_effect=_ruff_then_radon("", radon_json)):
        out = g.run("Write", {"file_path": str(py)})

    assert "complejidad alta" in out
    # Ordenados desc por CC: monster antes que spaghetti.
    assert out.index(f"monster (CC={hi})") < out.index(f"spaghetti (CC={mid})")
    assert "ok_fn" not in out, "función bajo umbral no debe aparecer"

    rows = _gate_rows(_isolated_root)
    details = json.loads(rows[0]["details"])
    assert details["lint"] == 0
    assert details["hotspots"] == 2
    assert details["worst_cc"] == hi


def test_radon_exactly_at_threshold_is_hotspot(tmp_path, _isolated_root) -> None:
    """CC == umbral cuenta como hotspot (>=)."""
    thr = g._COMPLEXITY_THRESHOLD
    radon_json = json.dumps({str(tmp_path / "t.py"): [{"name": "edge", "complexity": thr}]})
    py = tmp_path / "t.py"
    py.write_text("x = 1\n")

    with mock.patch.object(g.subprocess, "run", side_effect=_ruff_then_radon("", radon_json)):
        out = g.run("Write", {"file_path": str(py)})

    assert f"edge (CC={thr})" in out
    assert json.loads(_gate_rows(_isolated_root)[0]["details"])["hotspots"] == 1


def test_radon_just_below_threshold_is_clean(tmp_path, _isolated_root) -> None:
    """CC == umbral-1 (justo bajo umbral) → no hotspot → advisory vacío + clean."""
    radon_json = json.dumps(
        {str(tmp_path / "b.py"): [{"name": "almost", "complexity": g._COMPLEXITY_THRESHOLD - 1}]}
    )
    py = tmp_path / "b.py"
    py.write_text("x = 1\n")

    with mock.patch.object(g.subprocess, "run", side_effect=_ruff_then_radon("", radon_json)):
        out = g.run("Write", {"file_path": str(py)})

    assert out == ""
    assert _gate_rows(_isolated_root)[0]["status"] == "clean"


def test_combined_issues_and_hotspots(tmp_path, _isolated_root) -> None:
    """ruff + radon a la vez: ambas secciones en el advisory y en details."""
    ruff_out = f"{tmp_path}/c.py:1:1: F401 unused\n"
    radon_json = json.dumps({str(tmp_path / "c.py"): [{"name": "big", "complexity": 30}]})
    py = tmp_path / "c.py"
    py.write_text("x = 1\n")

    with mock.patch.object(g.subprocess, "run", side_effect=_ruff_then_radon(ruff_out, radon_json)):
        out = g.run("MultiEdit", {"file_path": str(py)})

    assert "ruff: 1 issue(s)" in out
    assert "big (CC=30)" in out
    details = json.loads(_gate_rows(_isolated_root)[0]["details"])
    assert details == {"lint": 1, "hotspots": 1, "worst_cc": 30}


# --------------------------------------------------------------------------- #
# Fail-open: binarios ausentes, subprocess que revienta, radon JSON inválido
# --------------------------------------------------------------------------- #


def test_fail_open_when_ruff_and_radon_missing(tmp_path, _isolated_root, monkeypatch) -> None:
    """Sin ruff/radon instalados → helpers devuelven [] → "" + registro clean."""
    monkeypatch.setattr(g, "_RUFF", str(tmp_path / "no_ruff_here"))
    monkeypatch.setattr(g, "_RADON", str(tmp_path / "no_radon_here"))
    py = tmp_path / "x.py"
    py.write_text("x = 1\n")

    with mock.patch.object(g.subprocess, "run") as run:
        out = g.run("Write", {"file_path": str(py)})

    assert out == "", "binario ausente debe degradar a vacío, no romper"
    run.assert_not_called()  # ni siquiera intenta correr el subprocess
    assert _gate_rows(_isolated_root)[0]["status"] == "clean"


def test_fail_open_when_subprocess_raises(tmp_path, _isolated_root) -> None:
    """subprocess.run lanza (timeout/OSError) → helper traga → "" + clean."""
    py = tmp_path / "x.py"
    py.write_text("x = 1\n")

    with mock.patch.object(
        g.subprocess, "run", side_effect=g.subprocess.TimeoutExpired("ruff", 10)
    ):
        out = g.run("Write", {"file_path": str(py)})

    assert out == ""
    assert _gate_rows(_isolated_root)[0]["status"] == "clean"


def test_fail_open_on_invalid_radon_json(tmp_path, _isolated_root) -> None:
    """radon devuelve JSON corrupto → _run_radon traga → sin hotspots."""
    py = tmp_path / "x.py"
    py.write_text("x = 1\n")

    with mock.patch.object(g.subprocess, "run", side_effect=_ruff_then_radon("", "not-json{{{")):
        out = g.run("Write", {"file_path": str(py)})

    assert out == ""
    assert json.loads(_gate_rows(_isolated_root)[0]["details"])["hotspots"] == 0


def test_radon_ignores_non_list_values(tmp_path, _isolated_root) -> None:
    """Valores no-lista en el JSON de radon se ignoran sin reventar."""
    cc = g._COMPLEXITY_THRESHOLD + 1
    radon_json = json.dumps(
        {
            "_meta": "garbage",  # no es lista → se salta
            str(tmp_path / "x.py"): [{"name": "f", "complexity": cc}],
        }
    )
    py = tmp_path / "x.py"
    py.write_text("x = 1\n")

    with mock.patch.object(g.subprocess, "run", side_effect=_ruff_then_radon("", radon_json)):
        out = g.run("Write", {"file_path": str(py)})

    assert f"f (CC={cc})" in out
    assert json.loads(_gate_rows(_isolated_root)[0]["details"])["hotspots"] == 1


# --------------------------------------------------------------------------- #
# _record fail-open: el advisory se devuelve aunque el DB no exista
# --------------------------------------------------------------------------- #


def test_record_noop_when_db_missing_still_returns_advisory(tmp_path, monkeypatch) -> None:
    """Si data/sessions.db no existe, _record es no-op pero el advisory se devuelve."""
    bare_root = tmp_path / "bare"
    bare_root.mkdir()  # sin data/sessions.db
    monkeypatch.setattr(g, "ARIS4U_ROOT", bare_root)
    py = tmp_path / "x.py"
    py.write_text("x = 1\n")
    ruff_out = f"{tmp_path}/x.py:1:1: F401 unused\n"

    with mock.patch.object(g.subprocess, "run", side_effect=_ruff_then_radon(ruff_out, "{}")):
        out = g.run("Write", {"file_path": str(py)})

    assert "ruff: 1 issue(s)" in out  # advisory intacto pese a no poder registrar
    assert not (bare_root / "data" / "sessions.db").exists()


def test_record_fail_open_on_db_error(tmp_path, _isolated_root, monkeypatch) -> None:
    """Un error de sqlite en _record no debe propagarse (best-effort)."""
    py = tmp_path / "x.py"
    py.write_text("x = 1\n")

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(g.sqlite3, "connect", boom)
    with mock.patch.object(g.subprocess, "run", return_value=_proc(stdout="")):
        out = g.run("Write", {"file_path": str(py)})  # no debe levantar

    assert out == ""


if __name__ == "__main__":
    import subprocess as _sp

    raise SystemExit(_sp.call([sys.executable, "-m", "pytest", __file__, "-q"]))
