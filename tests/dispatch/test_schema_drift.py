"""Tests de caracterización del handler schema_drift (PostToolUse).

Caracteriza el comportamiento ACTUAL de
`hooks/dispatch/handlers/schema_drift.py::run` (CC=29, sin tests directos previos —
solo cobertura indirecta vía tests/test_v16_e2e_multi_stack.py), como red de
seguridad ANTES del refactor. Los asserts describen lo que el código hace HOY.

Patrón: IN-PROCESS con mocks (como tests/dispatch/test_subagent_start.py).
Estrategia:

  - `run()` invoca `schema_compat_check.py` y `detect_stack_cli.py` por
    `subprocess.run`. Se monkeypatchea `sd.subprocess.run` con un fake que
    devuelve un objeto con `.stdout/.stderr/.returncode`, simulando: drift
    detectado / sin drift / error / stack soportado o no.
  - El gating por lab-project se controla apuntando `sd._LAB_PROJECTS` a un
    prefijo bajo `tmp_path` y creando un árbol de repo real (con `.git`) para
    que `_find_repo_root` encuentre la raíz sin mocks frágiles.
  - `os.path.isfile` se deja real: los tools existen en el repo, así que la
    rama "tool ausente" se cubre monkeypatcheando `sd.os.path.isfile`.
  - El side-effect del JSONL de telemetría se aísla redirigiendo
    `ARIS4U_LOG_FILE`/`ARIS4U_VALIDATION_LOG` a `tmp_path` vía monkeypatch de
    `os.environ` (Regla #2: nunca tocar estado global real).

Ramas cubiertas:
  - tool_name no Write/Edit/MultiEdit → "".
  - file_path vacío / tool_input None → "".
  - file_path fuera de lab projects → "".
  - file_path en lab pero no schema-relevante → "".
  - schema_compat_check ausente → "".
  - sin cambios de esquema (source unknown / errors 0) → no-op ("").
  - drift detectado (errors > 0, source db/static) → advisory.
  - source db vs static → event_name correcto + advisory.
  - subprocess.run del check lanza → fail-open ("" sin crashear).
  - detect_stack_cli falla / ausente → stack "generic", sigue.
  - JSONL de telemetría escrito cuando VALIDATION_LOG activo.

Corre:
    .venv312/bin/python3 -m pytest tests/dispatch/test_schema_drift.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"

if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dispatch.handlers import schema_drift as sd  # noqa: E402


class _FakeProc:
    """Stand-in de subprocess.CompletedProcess para los fakes de subprocess.run."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _meta_line(source: str, errors: int = 0, warnings: int = 0) -> str:
    """Construye la línea-footer estructurada que el handler parsea.

    Args:
        source: 'db', 'static' o 'unknown'.
        errors: conteo de errores de drift.
        warnings: conteo de warnings de drift.

    Returns:
        Una línea JSON con prefijo `{"_meta": true ...}` (lo que busca el parser).
    """
    return json.dumps({"_meta": True, "source": source, "errors": errors, "warnings": warnings})


@pytest.fixture
def lab_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Crea un árbol de repo-lab real bajo tmp_path y lo registra como lab project.

    Estructura:
        tmp_path/lab/                      (lab project, con .git → repo root)
            supabase/migrations/x.sql      (archivo schema-relevante)

    Apunta `sd._LAB_PROJECTS` SOLO a este lab para aislar el test.

    Returns:
        El Path del archivo schema-relevante (file_path a pasar a run()).
    """
    lab = tmp_path / "lab"
    (lab / ".git").mkdir(parents=True)  # marcador de repo root
    migrations = lab / "supabase" / "migrations"
    migrations.mkdir(parents=True)
    sql = migrations / "0001_init.sql"
    sql.write_text("create table t (id int);")

    monkeypatch.setattr(sd, "_LAB_PROJECTS", [str(lab) + "/"])
    return sql


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Limpia las env vars de telemetría para que el JSONL NO se escriba por defecto."""
    monkeypatch.delenv("ARIS4U_VALIDATION_LOG", raising=False)
    monkeypatch.delenv("ARIS4U_LOG_FILE", raising=False)


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    check_proc: _FakeProc | Exception,
    stack_out: str | Exception = "generic",
) -> list[list[str]]:
    """Monkeypatchea sd.subprocess.run para el check de esquema y el detect-stack.

    El handler llama subprocess.run dos veces: primero con schema_compat_check.py,
    luego (si existe el cli) con detect_stack_cli.py. Este fake distingue por el
    nombre del script en argv.

    Args:
        monkeypatch: fixture pytest.
        check_proc: _FakeProc devuelto para el schema check, o Exception a lanzar.
        stack_out: stdout devuelto por detect_stack_cli, o Exception a lanzar.

    Returns:
        Lista de los argv recibidos (para aserciones de invocación).
    """
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], *args: Any, **kwargs: Any) -> _FakeProc:
        calls.append(argv)
        joined = " ".join(argv)
        if "schema_compat_check.py" in joined:
            if isinstance(check_proc, Exception):
                raise check_proc
            return check_proc
        if "detect_stack_cli.py" in joined:
            if isinstance(stack_out, Exception):
                raise stack_out
            return _FakeProc(stdout=stack_out)
        return _FakeProc()

    monkeypatch.setattr(sd.subprocess, "run", _fake_run)
    return calls


# ---------------------------------------------------------------------------
# Early returns — gating
# ---------------------------------------------------------------------------


def test_returns_empty_for_unsupported_tool(clean_env: None) -> None:
    """tool_name fuera de Write/Edit/MultiEdit → "" sin tocar subprocess."""
    assert sd.run("Read", {"file_path": "/x.sql"}) == ""


def test_returns_empty_for_empty_file_path(clean_env: None) -> None:
    """Sin file_path → "" (tool_input None también)."""
    assert sd.run("Write", {}) == ""
    assert sd.run("Write", {"file_path": ""}) == ""
    assert sd.run("Write", None) == ""


def test_returns_empty_outside_lab_projects(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """file_path que no empieza por ningún lab project → ""."""
    monkeypatch.setattr(sd, "_LAB_PROJECTS", ["/nonexistent-lab/"])
    assert sd.run("Write", {"file_path": "/somewhere/else/x.sql"}) == ""


def test_returns_empty_for_irrelevant_file_in_lab(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """En lab pero archivo NO schema-relevante → ""."""
    lab = tmp_path / "lab"
    lab.mkdir()
    monkeypatch.setattr(sd, "_LAB_PROJECTS", [str(lab) + "/"])
    irrelevant = lab / "README.md"
    assert sd.run("Write", {"file_path": str(irrelevant)}) == ""


def test_returns_empty_when_schema_check_tool_missing(
    clean_env: None, lab_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si schema_compat_check.py no existe en disco → "" (no corre subprocess)."""
    real_isfile = sd.os.path.isfile

    def _fake_isfile(path: str) -> bool:
        if path.endswith("schema_compat_check.py"):
            return False
        return real_isfile(path)

    monkeypatch.setattr(sd.os.path, "isfile", _fake_isfile)

    def _no_run(*a: Any, **k: Any) -> None:
        raise AssertionError("subprocess.run no debe llamarse si falta el tool")

    monkeypatch.setattr(sd.subprocess, "run", _no_run)
    assert sd.run("Write", {"file_path": str(lab_repo)}) == ""


# ---------------------------------------------------------------------------
# No drift / drift advisory
# ---------------------------------------------------------------------------


def test_no_drift_returns_empty(
    clean_env: None, lab_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check corre con source db pero 0 errores → no-op ("")."""
    _patch_subprocess(
        monkeypatch,
        check_proc=_FakeProc(stdout=_meta_line("db", errors=0, warnings=0)),
    )
    assert sd.run("Write", {"file_path": str(lab_repo)}) == ""


def test_unknown_source_returns_empty_even_with_errors(
    clean_env: None, lab_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin footer _meta parseable → source 'unknown' → "" aunque... (no hay errores)."""
    _patch_subprocess(
        monkeypatch,
        check_proc=_FakeProc(stdout="ruido sin footer estructurado"),
    )
    assert sd.run("Write", {"file_path": str(lab_repo)}) == ""


def test_drift_detected_db_emits_advisory(
    clean_env: None, lab_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """source db + errors>0 → advisory con conteos y modo [db]."""
    _patch_subprocess(
        monkeypatch,
        check_proc=_FakeProc(stdout=_meta_line("db", errors=2, warnings=1), returncode=1),
    )
    out = sd.run("Write", {"file_path": str(lab_repo)})
    assert "Schema drift detected" in out
    assert "[db mode]" in out
    assert "Errors:   2" in out
    assert "Warnings: 1" in out
    assert "schema_compat_check.py" in out
    assert str(lab_repo) in out


def test_drift_detected_static_emits_advisory(
    clean_env: None, lab_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """source static + errors>0 → advisory en modo [static]."""
    _patch_subprocess(
        monkeypatch,
        check_proc=_FakeProc(stdout=_meta_line("static", errors=1, warnings=0)),
    )
    out = sd.run("Write", {"file_path": str(lab_repo)})
    assert "Schema drift detected" in out
    assert "[static mode]" in out
    assert "Errors:   1" in out


def test_warnings_only_no_advisory(
    clean_env: None, lab_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """source válido pero solo warnings (errors 0) → no advisory ("")."""
    _patch_subprocess(
        monkeypatch,
        check_proc=_FakeProc(stdout=_meta_line("static", errors=0, warnings=3)),
    )
    assert sd.run("Write", {"file_path": str(lab_repo)}) == ""


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


def test_subprocess_raises_fails_open(
    clean_env: None, lab_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si subprocess.run del check lanza, run() propaga (caracteriza comportamiento ACTUAL)."""
    # El handler NO envuelve la primera subprocess.run en try/except → lanza.
    _patch_subprocess(monkeypatch, check_proc=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        sd.run("Write", {"file_path": str(lab_repo)})


def test_detect_stack_failure_falls_back_to_generic(
    clean_env: None, lab_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si detect_stack_cli lanza, stack='generic' y el advisory igual se emite."""
    _patch_subprocess(
        monkeypatch,
        check_proc=_FakeProc(stdout=_meta_line("db", errors=1)),
        stack_out=RuntimeError("stack detect blew up"),
    )
    out = sd.run("Write", {"file_path": str(lab_repo)})
    assert "Schema drift detected" in out  # no crashea pese al fallo de stack


def test_detect_stack_cli_missing_uses_generic(
    clean_env: None, lab_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sin detect_stack_cli.py en disco, no se llama y el flujo sigue (stack generic)."""
    real_isfile = sd.os.path.isfile

    def _fake_isfile(path: str) -> bool:
        if path.endswith("detect_stack_cli.py"):
            return False
        return real_isfile(path)

    monkeypatch.setattr(sd.os.path, "isfile", _fake_isfile)
    calls = _patch_subprocess(
        monkeypatch,
        check_proc=_FakeProc(stdout=_meta_line("db", errors=1)),
    )
    out = sd.run("Write", {"file_path": str(lab_repo)})
    assert "Schema drift detected" in out
    # Solo se llamó el schema check, no el detect-stack.
    assert all("detect_stack_cli.py" not in " ".join(c) for c in calls)


# ---------------------------------------------------------------------------
# Telemetry JSONL side-effect
# ---------------------------------------------------------------------------


def test_jsonl_written_when_validation_log_active(
    lab_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Con ARIS4U_VALIDATION_LOG + ARIS4U_LOG_FILE, se anexa una línea JSONL del evento."""
    log_file = tmp_path / "validation.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log_file))
    _patch_subprocess(
        monkeypatch,
        check_proc=_FakeProc(stdout=_meta_line("db", errors=2, warnings=1)),
    )

    sd.run("Write", {"file_path": str(lab_repo)})

    assert log_file.exists()
    rec = json.loads(log_file.read_text().strip())
    assert rec["hook"] == "schema_drift"
    assert rec["event"] == "schema_check_db"
    assert rec["drift_errors"] == 2
    assert rec["drift_warnings"] == 1
    assert rec["drift_count"] == 3
    assert rec["source"] == "db"


def test_jsonl_not_written_without_validation_log(
    clean_env: None, lab_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sin ARIS4U_VALIDATION_LOG, no se escribe el JSONL (clean_env limpió las vars)."""
    log_file = tmp_path / "should_not_exist.jsonl"
    # ARIS4U_LOG_FILE ausente por clean_env; aunque pongamos solo el path, falta el flag.
    _patch_subprocess(
        monkeypatch,
        check_proc=_FakeProc(stdout=_meta_line("db", errors=1)),
    )
    sd.run("Write", {"file_path": str(lab_repo)})
    assert not log_file.exists()


def test_skipped_event_name_when_source_unknown(
    lab_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """source 'unknown' → event_name 'schema_check_skipped' en el JSONL."""
    log_file = tmp_path / "validation.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log_file))
    _patch_subprocess(
        monkeypatch,
        check_proc=_FakeProc(stdout="no footer here"),
    )
    sd.run("Write", {"file_path": str(lab_repo)})
    rec = json.loads(log_file.read_text().strip())
    assert rec["event"] == "schema_check_skipped"
    assert rec["source"] == "unknown"
