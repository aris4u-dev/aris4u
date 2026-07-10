"""Cobertura del handler commit_quality_gate — PostToolUse (Bash + `git commit`).

El gate corre en el COMMIT (no por-edit): pyright sobre los .py del commit + tests
AFECTADOS, y registra en `gate_results`. Es **advisory**: nunca bloquea (devuelve string
o ""), consistente con el contrato del dispatcher. Fail-open total.

Patrón copiado de tests/dispatch/test_post_tool_use.py (test_capture_commit_*):
el handler se importa directo y se invoca en-proceso `run(tool_name, tool_input, cwd)`.
Se mockea subprocess (pyright/pytest) y la DB se AISLA monkeypatcheando
`commit_quality_gate.ARIS4U_ROOT` a un tmp_path (las fixtures autouse del conftest
ya aíslan sessions.db/event-log; aquí además aislamos el DB propio de este handler).

Corre:  .venv312/bin/python3 -m pytest tests/dispatch/test_commit_quality_gate.py -q
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"

# Permite importar dispatch.* en-proceso (igual que test_post_tool_use.py).
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dispatch.handlers import commit_quality_gate as cqg  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_GATE_SCHEMA = """
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
"""


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """Aísla el DB del handler (`<ARIS4U_ROOT>/data/sessions.db`) a un tmp con la tabla.

    Devuelve la ruta al DB. Permite verificar lo que `_record` escribe sin tocar el
    sessions.db real. Crea data/sessions.db con la tabla gate_results.
    """
    fake_root = tmp_path / "fakeroot"
    (fake_root / "data").mkdir(parents=True)
    db = fake_root / "data" / "sessions.db"
    conn = sqlite3.connect(str(db))
    conn.execute(_GATE_SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(cqg, "ARIS4U_ROOT", fake_root)
    return db


def _gate_rows(db: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM gate_results ORDER BY id"))
    finally:
        conn.close()


def _stub_pipeline(
    monkeypatch,
    *,
    files: list[str] | None = None,
    type_errors: int = 0,
    pytest_bin: str = "/fake/pytest",
    tests: list[str] | None = None,
    passed: int = 0,
    failed: int = 0,
):
    """Sustituye las funciones de la tubería (changed/pyright/find/affected/run) por stubs.

    Mantiene `run()` corriendo de verdad (gating, formato advisory, _record), pero sin
    tocar git/pyright/pytest reales.
    """
    monkeypatch.setattr(cqg, "_changed_py_files", lambda repo: list(files or []))
    monkeypatch.setattr(cqg, "_run_pyright", lambda fs: type_errors)
    monkeypatch.setattr(cqg, "_find_pytest", lambda repo: pytest_bin)
    monkeypatch.setattr(cqg, "_affected_tests", lambda repo, fs: list(tests or []))
    monkeypatch.setattr(cqg, "_run_tests", lambda pb, ts: (passed, failed))


# ---------------------------------------------------------------------------
# Gating: commit vs no-commit
# ---------------------------------------------------------------------------


def test_non_bash_tool_is_noop(monkeypatch, isolated_db) -> None:
    """Cualquier tool que no sea Bash → "" y no toca la tubería ni el DB."""

    def boom(*a, **k):
        pytest.fail("la tubería no debe correr para no-Bash")

    monkeypatch.setattr(cqg, "_changed_py_files", boom)
    out = cqg.run("Write", {"command": "git commit -m x"}, "/tmp")
    assert out == ""
    assert _gate_rows(isolated_db) == []


def test_bash_without_git_commit_is_noop(monkeypatch, isolated_db) -> None:
    """Bash sin `git commit` en el comando → "" (p.ej. git status, git add)."""
    monkeypatch.setattr(
        cqg, "_changed_py_files", lambda repo: pytest.fail("no debe analizar")
    )
    assert cqg.run("Bash", {"command": "git status"}, "/tmp") == ""
    assert cqg.run("Bash", {"command": "git add -A"}, "/tmp") == ""
    assert cqg.run("Bash", {"command": "ls -la"}, "/tmp") == ""
    assert _gate_rows(isolated_db) == []


def test_empty_tool_input_is_noop(isolated_db) -> None:
    """tool_input None / sin command → "" (fail-open, no revienta)."""
    assert cqg.run("Bash", None, "/tmp") == ""  # type: ignore[arg-type]
    assert cqg.run("Bash", {}, "/tmp") == ""
    assert _gate_rows(isolated_db) == []


def test_commit_detected_but_no_py_files_is_noop(monkeypatch, isolated_db) -> None:
    """`git commit` real pero sin .py cambiados → "" y NO registra (early return)."""
    monkeypatch.setattr(cqg, "_changed_py_files", lambda repo: [])
    # Si llegara a pyright/record, fallaría: confirmamos que no avanza.
    monkeypatch.setattr(
        cqg, "_run_pyright", lambda fs: pytest.fail("no debe llegar a pyright")
    )
    out = cqg.run("Bash", {"command": "git commit -m 'chore: docs'"}, "/somerepo")
    assert out == ""
    assert _gate_rows(isolated_db) == []


# ---------------------------------------------------------------------------
# Camino limpio: registra "clean", advisory vacío
# ---------------------------------------------------------------------------


def test_clean_commit_records_clean_and_no_advisory(monkeypatch, isolated_db) -> None:
    """Sin errores de tipo y tests en verde → status 'clean', advisory "" (silencioso)."""
    _stub_pipeline(
        monkeypatch,
        files=["/repo/mod.py"],
        type_errors=0,
        tests=["/repo/test_mod.py"],
        passed=5,
        failed=0,
    )
    out = cqg.run("Bash", {"command": "git commit -m 'feat: x'"}, "/repo")
    assert out == "", "camino limpio no debe emitir advisory (no ser ruidoso)"

    rows = _gate_rows(isolated_db)
    assert len(rows) == 1
    assert rows[0]["status"] == "clean"
    assert rows[0]["module_name"] == "commit:repo"
    details = json.loads(rows[0]["details"])
    assert details == {
        "type_errors": 0,
        "tests_passed": 5,
        "tests_failed": 0,
        "n_files": 1,
    }
    # timestamp es ISO-8601 UTC válido
    assert rows[0]["timestamp"].endswith("+00:00")


def test_clean_without_tests_run_is_silent(monkeypatch, isolated_db) -> None:
    """Sin pytest en el repo (passed=-1) y sin type errors → clean + advisory "" ."""
    _stub_pipeline(
        monkeypatch,
        files=["/repo/a.py", "/repo/b.py"],
        type_errors=0,
        pytest_bin="",
        tests=[],
        passed=-1,
        failed=-1,
    )
    out = cqg.run("Bash", {"command": "git commit -m wip"}, "/repo")
    assert out == ""
    rows = _gate_rows(isolated_db)
    assert len(rows) == 1
    assert rows[0]["status"] == "clean"
    details = json.loads(rows[0]["details"])
    # max(-1,0)=0 → negativos (no-disponible) se normalizan a 0 en el registro.
    assert details["tests_passed"] == 0
    assert details["tests_failed"] == 0
    assert details["n_files"] == 2


# ---------------------------------------------------------------------------
# Issues: type errors / tests fallando → advisory bien formado, status 'issues'
# ---------------------------------------------------------------------------


def test_type_errors_produce_advisory(monkeypatch, isolated_db) -> None:
    """pyright>0 → advisory con la línea de pyright y status 'issues'."""
    _stub_pipeline(
        monkeypatch,
        files=["/repo/typed.py"],
        type_errors=3,
        tests=[],
        passed=-1,
        failed=-1,
    )
    out = cqg.run("Bash", {"command": "git commit -m 'feat: typed'"}, "/repo")
    assert out.startswith("🔎 Commit quality gate (1 archivo·s .py):")
    assert "pyright: 3 error(es) de tipo" in out

    rows = _gate_rows(isolated_db)
    assert len(rows) == 1
    assert rows[0]["status"] == "issues"
    details = json.loads(rows[0]["details"])
    assert details["type_errors"] == 3


def test_failing_tests_produce_advisory(monkeypatch, isolated_db) -> None:
    """tests fallando → advisory con conteo failed/pass y status 'issues'."""
    _stub_pipeline(
        monkeypatch,
        files=["/repo/svc.py"],
        type_errors=0,
        tests=["/repo/test_svc.py"],
        passed=4,
        failed=2,
    )
    out = cqg.run("Bash", {"command": "git commit -m 'fix: svc'"}, "/repo")
    assert "tests afectados: 2 FALLANDO (4 pass)" in out
    assert "revisar antes del próximo cambio" in out
    # con fallos NO debe aparecer la línea verde de "pass ✓"
    assert "pass ✓" not in out

    rows = _gate_rows(isolated_db)
    assert rows[0]["status"] == "issues"
    details = json.loads(rows[0]["details"])
    assert details["tests_failed"] == 2
    assert details["tests_passed"] == 4


def test_type_errors_and_failing_tests_both_listed(monkeypatch, isolated_db) -> None:
    """Ambos problemas → ambas líneas presentes en el advisory."""
    _stub_pipeline(
        monkeypatch,
        files=["/repo/a.py", "/repo/b.py"],
        type_errors=1,
        tests=["/repo/test_a.py"],
        passed=0,
        failed=3,
    )
    out = cqg.run("Bash", {"command": "git commit --amend"}, "/repo")
    assert "2 archivo·s .py" in out
    assert "pyright: 1 error(es)" in out
    assert "tests afectados: 3 FALLANDO" in out
    assert _gate_rows(isolated_db)[0]["status"] == "issues"


# ---------------------------------------------------------------------------
# Fail-open: DB ausente / record best-effort
# ---------------------------------------------------------------------------


def test_record_noop_when_db_absent(monkeypatch, tmp_path) -> None:
    """Si data/sessions.db NO existe, _record es no-op y run() igual emite advisory."""
    fake_root = tmp_path / "noroot"  # sin data/sessions.db
    fake_root.mkdir()
    monkeypatch.setattr(cqg, "ARIS4U_ROOT", fake_root)
    _stub_pipeline(
        monkeypatch,
        files=["/repo/x.py"],
        type_errors=2,
        tests=[],
        passed=-1,
        failed=-1,
    )
    # No revienta aunque no haya DB; el advisory se emite igual.
    out = cqg.run("Bash", {"command": "git commit -m x"}, "/repo")
    assert "pyright: 2 error(es)" in out
    assert not (fake_root / "data" / "sessions.db").exists()


def test_record_swallows_db_errors(monkeypatch, tmp_path) -> None:
    """Un error de sqlite en _record no se propaga (best-effort, fail-open)."""
    fake_root = tmp_path / "broken"
    (fake_root / "data").mkdir(parents=True)
    # Crea el archivo para pasar el .exists() pero sin la tabla gate_results
    # → el INSERT lanzará OperationalError, que _record debe tragarse.
    db = fake_root / "data" / "sessions.db"
    sqlite3.connect(str(db)).close()
    monkeypatch.setattr(cqg, "ARIS4U_ROOT", fake_root)
    _stub_pipeline(
        monkeypatch, files=["/repo/x.py"], type_errors=1, tests=[], passed=-1, failed=-1
    )
    out = cqg.run("Bash", {"command": "git commit -m x"}, "/repo")
    assert "pyright: 1 error(es)" in out  # no se propagó la excepción del INSERT


# ---------------------------------------------------------------------------
# _run_pyright: fail-open si pyright no está / subprocess falla
# ---------------------------------------------------------------------------


def test_run_pyright_returns_minus1_when_not_installed(monkeypatch) -> None:
    """pyright no en PATH → -1 (no disponible)."""
    monkeypatch.setattr(cqg.shutil, "which", lambda exe: None)
    assert cqg._run_pyright(["/repo/x.py"]) == -1


def test_run_pyright_returns_minus1_with_no_files(monkeypatch) -> None:
    """Sin archivos → -1 (aunque pyright exista)."""
    monkeypatch.setattr(cqg.shutil, "which", lambda exe: "/usr/bin/pyright")
    assert cqg._run_pyright([]) == -1


def test_run_pyright_parses_error_count(monkeypatch) -> None:
    """Con pyright presente, parsea summary.errorCount del JSON."""
    monkeypatch.setattr(cqg.shutil, "which", lambda exe: "/usr/bin/pyright")
    fake = subprocess.CompletedProcess(
        args=[], returncode=1,
        stdout=json.dumps({"summary": {"errorCount": 7}}), stderr="",
    )
    monkeypatch.setattr(cqg.subprocess, "run", lambda *a, **k: fake)
    assert cqg._run_pyright(["/repo/x.py"]) == 7


def test_run_pyright_failopen_on_bad_json(monkeypatch) -> None:
    """JSON inválido / subprocess que lanza → -1 (fail-open, no revienta)."""
    monkeypatch.setattr(cqg.shutil, "which", lambda exe: "/usr/bin/pyright")
    bad = subprocess.CompletedProcess(args=[], returncode=2, stdout="not json", stderr="")
    monkeypatch.setattr(cqg.subprocess, "run", lambda *a, **k: bad)
    assert cqg._run_pyright(["/repo/x.py"]) == -1

    def boom(*a, **k):
        raise OSError("pyright crashed")

    monkeypatch.setattr(cqg.subprocess, "run", boom)
    assert cqg._run_pyright(["/repo/x.py"]) == -1


# ---------------------------------------------------------------------------
# _run_tests: parseo de salida + fail-open
# ---------------------------------------------------------------------------


def test_run_tests_returns_minus1_without_bin_or_tests() -> None:
    """Sin pytest_bin o sin tests → (-1,-1)."""
    assert cqg._run_tests("", ["/repo/test_x.py"]) == (-1, -1)
    assert cqg._run_tests("/fake/pytest", []) == (-1, -1)


def test_run_tests_parses_passed_and_failed(monkeypatch) -> None:
    """Parsea 'N passed' y 'N failed' de la salida combinada de pytest."""
    fake = subprocess.CompletedProcess(
        args=[], returncode=1,
        stdout="...F.\n3 passed, 1 failed in 0.42s\n", stderr="",
    )
    monkeypatch.setattr(cqg.subprocess, "run", lambda *a, **k: fake)
    assert cqg._run_tests("/fake/pytest", ["/repo/test_x.py"]) == (3, 1)


def test_run_tests_all_green_zero_failed(monkeypatch) -> None:
    """Solo 'N passed' (sin failed) → (N, 0)."""
    fake = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="6 passed in 0.10s\n", stderr=""
    )
    monkeypatch.setattr(cqg.subprocess, "run", lambda *a, **k: fake)
    assert cqg._run_tests("/fake/pytest", ["/repo/test_x.py"]) == (6, 0)


def test_run_tests_failopen_on_exception(monkeypatch) -> None:
    """subprocess.run lanza (p.ej. timeout) → (-1,-1)."""
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=60)

    monkeypatch.setattr(cqg.subprocess, "run", boom)
    assert cqg._run_tests("/fake/pytest", ["/repo/test_x.py"]) == (-1, -1)


# ---------------------------------------------------------------------------
# _find_pytest: localiza el venv del repo
# ---------------------------------------------------------------------------


def test_find_pytest_locates_venv_bin(tmp_path) -> None:
    """Encuentra .venv*/bin/pytest dentro del repo."""
    repo = tmp_path / "repo"
    venv_bin = repo / ".venv312" / "bin"
    venv_bin.mkdir(parents=True)
    pytest_bin = venv_bin / "pytest"
    pytest_bin.write_text("#!/bin/sh\n")
    found = cqg._find_pytest(str(repo))
    assert found.endswith("/bin/pytest")
    assert Path(found).exists()


def test_find_pytest_returns_empty_when_absent(tmp_path) -> None:
    """Repo sin venv → "" ."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert cqg._find_pytest(str(repo)) == ""


# ---------------------------------------------------------------------------
# _changed_py_files: filtrado de rutas + fail-open de git
# ---------------------------------------------------------------------------


def test_changed_py_files_filters_and_resolves(tmp_path, monkeypatch) -> None:
    """Solo .py existentes; ignora venv/site-packages/node_modules y no-.py."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    real = repo / "pkg" / "mod.py"
    real.write_text("x = 1\n")
    # README.md no es .py; venv/lib/foo.py está en SKIP; ghost.py no existe en disco.
    git_out = "pkg/mod.py\nREADME.md\n.venv/lib/foo.py\nghost.py\n"
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout=git_out, stderr="")
    monkeypatch.setattr(cqg.subprocess, "run", lambda *a, **k: fake)

    files = cqg._changed_py_files(str(repo))
    assert files == [str(real)]


def test_changed_py_files_failopen_on_git_error(monkeypatch) -> None:
    """git falla (subprocess lanza) → [] (fail-open)."""
    def boom(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(cqg.subprocess, "run", boom)
    assert cqg._changed_py_files("/no/repo") == []


# ---------------------------------------------------------------------------
# _affected_tests: mapeo módulo → test_<nombre>.py
# ---------------------------------------------------------------------------


def test_affected_tests_maps_module_to_test(tmp_path) -> None:
    """Para foo.py busca test_foo.py recursivamente en el repo."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir(parents=True)
    foo = repo / "src" / "foo.py"
    foo.write_text("def f(): ...\n")
    test_foo = repo / "tests" / "test_foo.py"
    test_foo.write_text("def test_f(): ...\n")

    out = cqg._affected_tests(str(repo), [str(foo)])
    assert str(test_foo) in out


def test_affected_tests_includes_test_files_directly(tmp_path) -> None:
    """Un archivo que YA es test_*.py se incluye a sí mismo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    t = repo / "test_thing.py"
    t.write_text("def test_x(): ...\n")
    out = cqg._affected_tests(str(repo), [str(t)])
    assert str(t) in out


def test_affected_tests_empty_when_no_match(tmp_path) -> None:
    """Módulo sin test correspondiente → set vacío."""
    repo = tmp_path / "repo"
    repo.mkdir()
    lonely = repo / "lonely.py"
    lonely.write_text("x = 1\n")
    assert cqg._affected_tests(str(repo), [str(lonely)]) == []


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
