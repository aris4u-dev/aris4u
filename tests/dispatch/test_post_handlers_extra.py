"""Cobertura extra de 3 handlers PostToolUse (advisory + fail-open).

Complementa tests/dispatch/test_post_tool_use.py (que prueba equivalencia vs .sh viejo
vía golden). Aquí se ejercita el COMPORTAMIENTO real de cada handler importándolo directo
(patrón de los tests de side-effect en test_post_tool_use.py), mockeando subprocess/red:

  - schema_drift.run            (Write/Edit/MultiEdit): vigía drift multi-stack; avisa SOLO
                                 si el check corrió (source != unknown) Y drift_errors > 0;
                                 nunca bloquea; emite telemetría JSONL si el validation log
                                 está activo; fail-open ante cualquier excepción.
  - parallel_dispatch_guard.check (Write .sh/.bash): sugiere paralelizar ssh w[N] secuenciales;
                                 advisory puro (devuelve string o "").
  - agent_dispatched.run        (Agent/Task): snapshot git HEAD de repos-lab a JSONL, SOLO con
                                 ARIS4U_VALIDATION_LOG + ARIS4U_LOG_FILE; no-op si no aplica.

Las fixtures autouse de conftest (_isolate_sessions_db, _isolate_event_log) protegen los DBs
y logs reales; estos tests además mockean subprocess/red para no tocar git/red/herramientas.

Corre:  .venv312/bin/python3 -m pytest tests/dispatch/test_post_handlers_extra.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"

# Permite importar dispatch.* en-proceso (mismo patrón que test_post_tool_use.py).
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from collections.abc import Callable  # noqa: E402

from dispatch.handlers import agent_dispatched, parallel_dispatch_guard, schema_drift  # noqa: E402

# Path de prueba genérico — NO depende de los proyectos-lab del operador.
# Los tests parchean schema_drift._LAB_PROJECTS con este valor vía _patch_lab (autouse).
_TEST_LAB_ROOT = "/tmp/testproj/"


@pytest.fixture(autouse=True)
def _patch_lab(monkeypatch: pytest.MonkeyPatch) -> None:
    """Aísla schema_drift._LAB_PROJECTS del entorno del operador para todos los tests del módulo.

    Los defaults de _LAB_PROJECTS dependen de config.json (vacíos si no hay config),
    así que cualquier test que use _RELEVANT_FILE necesita este patch para que el gating
    reconozca la ruta. Se aplica a todos los tests del módulo (autouse=True) sin efecto
    secundario en los tests de agent_dispatched/parallel_dispatch_guard (no usan _LAB_PROJECTS).
    """
    monkeypatch.setattr(schema_drift, "_LAB_PROJECTS", [_TEST_LAB_ROOT])


# ===========================================================================
# Helpers para mockear subprocess.run dentro de schema_drift
# ===========================================================================

class _FakeProc:
    """Stand-in mínimo del CompletedProcess que schema_drift consume."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _meta_line(source: str, errors: int = 0, warnings: int = 0) -> str:
    """Footer estructurado que parsea schema_drift (última línea {"_meta": true, ...})."""
    return json.dumps({"_meta": True, "source": source, "errors": errors, "warnings": warnings})


def _patch_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    schema_out: str,
    *,
    stack: str = "flutter",
    schema_rc: int = 0,
) -> None:
    """Patchea subprocess.run en el namespace de schema_drift.

    La primera invocación (sin timeout) corresponde a schema_compat_check; las que llevan
    el CLI detect_stack_cli.py devuelven el `stack` indicado. Distinguimos por el script
    invocado (argv[1]).
    """

    def fake_run(cmd: list, *args: object, **kwargs: object) -> _FakeProc:
        script = cmd[1] if len(cmd) > 1 else ""
        if script.endswith("detect_stack_cli.py"):
            return _FakeProc(stdout=stack + "\n")
        # schema_compat_check.py
        return _FakeProc(stdout=schema_out, returncode=schema_rc)

    monkeypatch.setattr(schema_drift.subprocess, "run", fake_run)


_RELEVANT_FILE = _TEST_LAB_ROOT + "src/main/resources/db/migration/V1__init.sql"


# ===========================================================================
# schema_drift — gating (no aplica)
# ===========================================================================

def test_schema_drift_ignores_non_edit_tools(monkeypatch) -> None:
    """Solo Write/Edit/MultiEdit; cualquier otro tool → "" sin tocar subprocess."""
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("subprocess no debe correr para tools no-edit")

    monkeypatch.setattr(schema_drift.subprocess, "run", boom)
    assert schema_drift.run("Bash", {"file_path": _RELEVANT_FILE}) == ""
    assert schema_drift.run("Read", {"file_path": _RELEVANT_FILE}) == ""


def test_schema_drift_empty_file_path(monkeypatch) -> None:
    """Sin file_path → "" temprano."""
    monkeypatch.setattr(schema_drift.subprocess, "run", lambda *a, **k: _FakeProc())
    assert schema_drift.run("Write", {}) == ""
    assert schema_drift.run("Write", {"file_path": ""}) == ""


def test_schema_drift_ignores_non_lab_path(monkeypatch) -> None:
    """Un archivo fuera de los proyectos-lab no dispara el check."""
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("subprocess no debe correr fuera de proyectos-lab")

    monkeypatch.setattr(schema_drift.subprocess, "run", boom)
    out = schema_drift.run(
        "Write",
        {"file_path": "/tmp/some/other/migration/V1__x.sql"},
    )
    assert out == ""


def test_schema_drift_ignores_irrelevant_file_in_lab(monkeypatch) -> None:
    """Archivo dentro de lab pero no schema-relevante (README) → "" sin subprocess."""
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("subprocess no debe correr para archivos no-relevantes")

    monkeypatch.setattr(schema_drift.subprocess, "run", boom)
    out = schema_drift.run("Write", {"file_path": _TEST_LAB_ROOT + "README.md"})
    assert out == ""


def test_schema_drift_skips_when_tool_missing(monkeypatch) -> None:
    """Si schema_compat_check.py no existe → "" (no corre subprocess)."""
    monkeypatch.setattr(schema_drift.os.path, "isfile", lambda p: False)

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("subprocess no debe correr si falta el tool")

    monkeypatch.setattr(schema_drift.subprocess, "run", boom)
    out = schema_drift.run("Write", {"file_path": _RELEVANT_FILE})
    assert out == ""


# ===========================================================================
# schema_drift — comportamiento advisory (warn / no-warn) + telemetría
# ===========================================================================

def test_schema_drift_warns_on_real_errors(monkeypatch) -> None:
    """source=db + errores reales → string de advertencia (pero retorno != bloqueo)."""
    schema_out = "some check output\n" + _meta_line("db", errors=2, warnings=1)
    _patch_subprocess(monkeypatch, schema_out, stack="flutter")

    warn = schema_drift.run("Write", {"file_path": _RELEVANT_FILE})
    assert "Schema drift detected" in warn
    assert "[db mode]" in warn
    assert "Errors:   2" in warn
    assert "Warnings: 1" in warn
    assert _RELEVANT_FILE in warn
    # Es un string para stderr — NO un dict de decisión que bloquee.
    assert isinstance(warn, str)


def test_schema_drift_no_warn_when_only_warnings(monkeypatch) -> None:
    """Warnings>0 pero errors=0 → NO avisa (umbral = drift_errors > 0)."""
    schema_out = _meta_line("static", errors=0, warnings=5)
    _patch_subprocess(monkeypatch, schema_out, stack="spring")
    assert schema_drift.run("Edit", {"file_path": _RELEVANT_FILE}) == ""


def test_schema_drift_no_warn_when_source_unknown(monkeypatch) -> None:
    """Sin footer _meta parseable, source=unknown → nunca avisa aunque 'parezca' roto."""
    schema_out = "boom: something failed but no _meta footer\n"
    _patch_subprocess(monkeypatch, schema_out, stack="generic", schema_rc=1)
    assert schema_drift.run("Write", {"file_path": _RELEVANT_FILE}) == ""


def test_schema_drift_no_warn_when_clean(monkeypatch) -> None:
    """source=db, 0 errores → check corrió limpio, sin advertencia."""
    schema_out = _meta_line("db", errors=0, warnings=0)
    _patch_subprocess(monkeypatch, schema_out, stack="flutter")
    assert schema_drift.run("Write", {"file_path": _RELEVANT_FILE}) == ""


def test_schema_drift_emits_telemetry_when_log_active(monkeypatch, tmp_path) -> None:
    """Con ARIS4U_VALIDATION_LOG + ARIS4U_LOG_FILE escribe UNA línea JSONL de telemetría."""
    log = tmp_path / "validation.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))

    schema_out = _meta_line("db", errors=3, warnings=0)
    _patch_subprocess(monkeypatch, schema_out, stack="flutter")

    schema_drift.run("Write", {"file_path": _RELEVANT_FILE})

    assert log.exists(), "schema_drift debe escribir telemetría con el log activo"
    ev = json.loads(log.read_text().strip().splitlines()[-1])
    assert ev["hook"] == "schema_drift"
    assert ev["event"] == "schema_check_db"  # source=db
    assert ev["drift_errors"] == 3
    assert ev["drift_count"] == 3
    assert ev["stack"] == "flutter"
    assert ev["trigger_file"].endswith("V1__init.sql")


def test_schema_drift_event_name_per_source(monkeypatch, tmp_path) -> None:
    """source static/unknown mapean a schema_check_static / schema_check_skipped."""
    log = tmp_path / "v.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))

    # static
    _patch_subprocess(monkeypatch, _meta_line("static", errors=0, warnings=2), stack="prisma")
    schema_drift.run("Write", {"file_path": _RELEVANT_FILE})
    ev = json.loads(log.read_text().strip().splitlines()[-1])
    assert ev["event"] == "schema_check_static"

    # unknown (sin footer)
    _patch_subprocess(monkeypatch, "no footer here", stack="generic")
    schema_drift.run("Write", {"file_path": _RELEVANT_FILE})
    ev = json.loads(log.read_text().strip().splitlines()[-1])
    assert ev["event"] == "schema_check_skipped"


def test_schema_drift_no_telemetry_without_log(monkeypatch, tmp_path) -> None:
    """Sin ARIS4U_VALIDATION_LOG no escribe el archivo de telemetría."""
    log = tmp_path / "nope.jsonl"
    monkeypatch.delenv("ARIS4U_VALIDATION_LOG", raising=False)
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))

    _patch_subprocess(monkeypatch, _meta_line("db", errors=1), stack="flutter")
    schema_drift.run("Write", {"file_path": _RELEVANT_FILE})
    assert not log.exists()


def test_schema_drift_telemetry_failopen_on_bad_logfile(monkeypatch) -> None:
    """Un ARIS4U_LOG_FILE no escribible NO debe propagar excepción (fail-open)."""
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    # Directorio inexistente → open() falla; el handler traga la excepción.
    monkeypatch.setenv("ARIS4U_LOG_FILE", "/nonexistent_dir_xyz/cant/write.jsonl")
    _patch_subprocess(monkeypatch, _meta_line("db", errors=1), stack="flutter")

    # No debe lanzar; aún devuelve la advertencia porque hubo errores.
    warn = schema_drift.run("Write", {"file_path": _RELEVANT_FILE})
    assert "Schema drift detected" in warn


def test_schema_drift_failopen_when_subprocess_raises(monkeypatch) -> None:
    """Si schema_compat_check revienta, el handler propaga la excepción del subprocess.

    El fail-open real lo aporta el orquestador (_safe en post_tool_use.handle), así que
    aquí verificamos que la primera llamada (sin try/except interno) levanta — y que el
    wrapper _safe la absorbería. Lo demostramos llamando vía _safe-equivalente.
    """
    def fake_run(cmd: list, *a: object, **k: object) -> _FakeProc:
        script = cmd[1] if len(cmd) > 1 else ""
        if script.endswith("detect_stack_cli.py"):
            return _FakeProc(stdout="flutter\n")
        raise RuntimeError("schema_compat_check exploded")

    monkeypatch.setattr(schema_drift.subprocess, "run", fake_run)

    # Llamada directa: la excepción del check NO está envuelta dentro de run() → propaga.
    with pytest.raises(RuntimeError):
        schema_drift.run("Write", {"file_path": _RELEVANT_FILE})

    # Y el patrón fail-open del orquestador (try/except → None) la absorbe.
    def _safe(fn: Callable[..., object], *args: object) -> object:
        try:
            return fn(*args)
        except Exception:
            return None

    assert _safe(schema_drift.run, "Write", {"file_path": _RELEVANT_FILE}) is None


def test_schema_drift_detect_stack_timeout_failopen(monkeypatch) -> None:
    """Si detect_stack_cli revienta/timeout, el stack cae a 'generic' sin tumbar el warn."""
    def fake_run(cmd: list, *a: object, **k: object) -> _FakeProc:
        script = cmd[1] if len(cmd) > 1 else ""
        if script.endswith("detect_stack_cli.py"):
            raise schema_drift.subprocess.TimeoutExpired(cmd, 10)
        return _FakeProc(stdout=_meta_line("db", errors=1), returncode=0)

    monkeypatch.setattr(schema_drift.subprocess, "run", fake_run)
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    log = Path(__import__("tempfile").mkstemp(suffix=".jsonl")[1])
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))

    warn = schema_drift.run("Write", {"file_path": _RELEVANT_FILE})
    assert "Schema drift detected" in warn
    ev = json.loads(log.read_text().strip().splitlines()[-1])
    assert ev["stack"] == "generic"  # cayó al default tras la excepción
    log.unlink(missing_ok=True)


# ===========================================================================
# parallel_dispatch_guard — advisory sobre scripts .sh
# ===========================================================================

def test_parallel_guard_ignores_non_shell_files() -> None:
    """Solo .sh/.bash; un .py con ssh secuencial no dispara el guard."""
    content = "ssh w1 do_a\nssh w2 do_b\n"
    assert parallel_dispatch_guard.check("Write", {"file_path": "deploy.py", "content": content}) == ""
    assert parallel_dispatch_guard.check("Write", {"file_path": "x.txt", "content": content}) == ""


def test_parallel_guard_flags_sequential_ssh() -> None:
    """Dos ssh w[N] sin '&' → advisory con el conteo."""
    content = "#!/bin/bash\nssh w1 'build'\nssh w2 'test'\n"
    out = parallel_dispatch_guard.check("Write", {"file_path": "deploy.sh", "content": content})
    assert "PARALLEL DISPATCH: 2 sequential ssh" in out
    assert "&" in out  # sugiere el patrón con ampersand


def test_parallel_guard_no_flag_when_backgrounded() -> None:
    """ssh con '&' final = ya paralelizado → sin advisory."""
    content = "#!/bin/bash\nssh w1 'build' &\nssh w2 'test' &\nwait\n"
    assert parallel_dispatch_guard.check("Write", {"file_path": "deploy.bash", "content": content}) == ""


def test_parallel_guard_ignores_commented_ssh() -> None:
    """Líneas comentadas con ssh no cuentan como violación."""
    content = "#!/bin/bash\n# ssh w1 old_way\n#   ssh w2 also old\necho hi\n"
    assert parallel_dispatch_guard.check("Write", {"file_path": "deploy.sh", "content": content}) == ""


def test_parallel_guard_empty_content() -> None:
    """Script .sh sin contenido (o sin tool_input) → "" (no crashea)."""
    assert parallel_dispatch_guard.check("Write", {"file_path": "empty.sh"}) == ""
    assert parallel_dispatch_guard.check("Write", {"file_path": "empty.sh", "content": ""}) == ""
    assert parallel_dispatch_guard.check("Write", None) == ""  # type: ignore[arg-type]


def test_parallel_guard_single_ssh_flagged() -> None:
    """Una sola llamada secuencial también cuenta (violations=1)."""
    content = "#!/bin/bash\nssh w2 'long-build'\necho done\n"
    out = parallel_dispatch_guard.check("Edit", {"file_path": "run.sh", "content": content})
    assert "1 sequential ssh" in out


def test_parallel_guard_stops_counting_after_wait() -> None:
    """Tras un `wait` con violaciones acumuladas deja de contar (rompe el loop)."""
    content = (
        "#!/bin/bash\n"
        "ssh w1 'a'\n"      # violación 1
        "ssh w2 'b'\n"      # violación 2
        "wait\n"            # corta el conteo (rompe)
        "ssh w3 'c'\n"      # NO se cuenta
    )
    out = parallel_dispatch_guard.check("Write", {"file_path": "p.sh", "content": content})
    assert "PARALLEL DISPATCH: 2 sequential ssh" in out


# ===========================================================================
# agent_dispatched — snapshot JSONL (solo con validation log)
# ===========================================================================

def _patch_git_heads(monkeypatch: pytest.MonkeyPatch, head: str = "deadbeefcafe") -> None:
    """Hace que cualquier repo-lab parezca tener .git + un HEAD fijo (sin tocar git real)."""
    monkeypatch.setattr(agent_dispatched.os.path, "isdir", lambda p: p.endswith(".git"))
    # CI hermeticity: _lab_repos() reads ~/.aris4u/config.json; returns [] without config.
    # Inject a fake repo so repo_heads_pre is non-empty in tests that assert on it.
    monkeypatch.setattr(agent_dispatched, "_lab_repos", lambda: ["/tmp/fake-testrepo"])

    def fake_run(cmd: list, *a: object, **k: object) -> _FakeProc:
        return _FakeProc(stdout=head + "\n", returncode=0)

    monkeypatch.setattr(agent_dispatched.subprocess, "run", fake_run)


def test_agent_dispatched_writes_snapshot(monkeypatch, tmp_path) -> None:
    """Agent + validation log → JSONL con subagent_type/prompt_preview/repo_heads_pre."""
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))
    _patch_git_heads(monkeypatch, head="abc123")

    inp = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "qa-agent", "prompt": "run the suite"},
    }
    agent_dispatched.run("Agent", inp)

    assert log.exists()
    ev = json.loads(log.read_text().strip().splitlines()[-1])
    assert ev["event"] == "agent_dispatched"
    assert ev["subagent_type"] == "qa-agent"
    assert ev["prompt_preview"] == "run the suite"
    assert isinstance(ev["repo_heads_pre"], dict)
    # Todos los repos-lab parecían tener .git → al menos uno con el HEAD fijo.
    assert any(v == "abc123" for v in ev["repo_heads_pre"].values())


def test_agent_dispatched_falls_back_to_description(monkeypatch, tmp_path) -> None:
    """Sin 'prompt' usa 'description' para prompt_preview; trunca a 200 chars."""
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))
    _patch_git_heads(monkeypatch)

    long_desc = "x" * 500
    inp = {"tool_name": "Task", "tool_input": {"description": long_desc}}
    agent_dispatched.run("Task", inp)

    ev = json.loads(log.read_text().strip().splitlines()[-1])
    assert ev["subagent_type"] == "unknown"  # default cuando no viene
    assert ev["prompt_preview"] == "x" * 200  # truncado


def test_agent_dispatched_noop_for_non_agent_tools(monkeypatch, tmp_path) -> None:
    """Bash/Write no son Agent/Task → no escribe nada."""
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))
    _patch_git_heads(monkeypatch)

    agent_dispatched.run("Bash", {"tool_name": "Bash", "tool_input": {}})
    agent_dispatched.run("Write", {"tool_name": "Write", "tool_input": {}})
    assert not log.exists()


def test_agent_dispatched_noop_without_validation_log(monkeypatch, tmp_path) -> None:
    """Sin ARIS4U_VALIDATION_LOG no escribe (gated igual que el .sh)."""
    log = tmp_path / "events.jsonl"
    monkeypatch.delenv("ARIS4U_VALIDATION_LOG", raising=False)
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))
    _patch_git_heads(monkeypatch)

    agent_dispatched.run("Agent", {"tool_name": "Agent", "tool_input": {"subagent_type": "x"}})
    assert not log.exists()


def test_agent_dispatched_noop_without_log_file(monkeypatch, tmp_path) -> None:
    """ARIS4U_VALIDATION_LOG activo pero sin ARIS4U_LOG_FILE → no-op."""
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.delenv("ARIS4U_LOG_FILE", raising=False)
    # No debe lanzar ni intentar escribir.
    agent_dispatched.run("Agent", {"tool_name": "Agent", "tool_input": {"subagent_type": "x"}})


def test_agent_dispatched_skips_repos_without_git(monkeypatch, tmp_path) -> None:
    """Si ningún repo-lab tiene .git, repo_heads_pre queda vacío (pero sí escribe el evento)."""
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))
    monkeypatch.setattr(agent_dispatched.os.path, "isdir", lambda p: False)

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("git rev-parse no debe correr sin .git")

    monkeypatch.setattr(agent_dispatched.subprocess, "run", boom)

    agent_dispatched.run("Agent", {"tool_name": "Agent", "tool_input": {"subagent_type": "z"}})
    ev = json.loads(log.read_text().strip().splitlines()[-1])
    assert ev["repo_heads_pre"] == {}


def test_agent_dispatched_failopen_on_git_error(monkeypatch, tmp_path) -> None:
    """git rev-parse que revienta en un repo no debe tumbar el handler (continue)."""
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))
    monkeypatch.setattr(agent_dispatched.os.path, "isdir", lambda p: p.endswith(".git"))

    def fake_run(cmd: list, *a: object, **k: object) -> _FakeProc:
        raise OSError("git not available")

    monkeypatch.setattr(agent_dispatched.subprocess, "run", fake_run)

    # No debe lanzar; el evento se escribe con repo_heads_pre vacío.
    agent_dispatched.run("Agent", {"tool_name": "Agent", "tool_input": {"subagent_type": "z"}})
    ev = json.loads(log.read_text().strip().splitlines()[-1])
    assert ev["event"] == "agent_dispatched"
    assert ev["repo_heads_pre"] == {}


if __name__ == "__main__":
    import subprocess as _sp

    PY = sys.executable
    sys.exit(_sp.call([PY, "-m", "pytest", __file__, "-v"]))
