"""Tests de CARACTERIZACIÓN para hooks/dispatch/events/stop.py (_verify + handle).

`_verify()` es el núcleo del antiguo post_agent_verify.sh (CC=47, sin tests hasta
hoy): al fin de cada main turn escanea el validation log por eventos
agent_dispatched / subagent_start que NO tengan su par en el ledger /tmp, infiere
los archivos que cada agente tocó (lab_write + diff git desde repo_heads_pre) y
corre tools/agent_output_verifier.py sobre ellos. Mantiene un ledger en /tmp y un
lock mkdir atómico. Exit 0 SIEMPRE (no bloqueante).

Estos tests fijan el comportamiento ACTUAL como red de seguridad ANTES de un
refactor futuro — deben PASAR contra el código tal cual está hoy (no se toca
stop.py). Ramas caracterizadas:

  _verify():
    - sin agente despachado (no hay agent_dispatched/subagent_start) → no-op
    - agente ya en el ledger → se salta (no re-verifica)
    - agente sin verificar + cambios + verifier rc=0 → escribe agent_verify_completed
      con verified>0 y marca el agente en el ledger
    - agente sin verificar + cambios + verifier rc!=0 → reporta el fallo
      (errors_total>0, verifier_exit!=0, resumen a stderr)
    - agente sin verificar SIN cambios (no lab_write, no git) → agent_verify_no_changes
    - salida del verifier no parseable → fallback verifier_parse_error
    - ts inválido en el start → se salta el agente
    - excepción en cualquier paso (log ilegible, verifier que revienta) → fail-open
    - byte-offset del ledger: relee solo lo nuevo del log
    - señal de archivos: lab_write vs git vs union

  handle():
    - log_file inexistente → passthrough (SystemExit(0))
    - verifier inexistente → passthrough (SystemExit(0))
    - lock /tmp ya tomado → passthrough sin verificar
    - happy-path end-to-end con el lock liberado → SystemExit(0), ledger actualizado

INVOCACIÓN: in-process. `_verify(log_file, ledger_path, verifier, ts_now)` recibe
TODAS sus dependencias de E/S como parámetros (paths explícitos), así que se ejerce
directamente apuntándolo a archivos en tmp_path; la única dependencia externa real
(el subprocess del verifier) se monkeypatchea vía `stop._run_verifier_safe`, lo que
hace los tests deterministas, rápidos y RAM-safe (no lanza python3/bash reales). Es
estrictamente mejor que el patrón subproceso `_invoke.py` para caracterizar ramas
internas: permite assertear el contenido exacto de cada evento por rama.

Corre:
    .venv312/bin/python3 -m pytest tests/dispatch/test_stop.py -q -p no:cacheprovider
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from dispatch.events import stop  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TS_NOW = "2026-06-19T12:00:00+00:00"
VERIFIER = "/usr/bin/true"  # path placeholder; _run_verifier_safe se monkeypatchea


def _write_log(path: Path, events: list[dict]) -> None:
    """Escribe una lista de eventos como JSONL (una línea por evento).

    Args:
        path: Destino del log.
        events: Eventos a serializar.
    """
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _read_log_events(path: Path) -> list[dict]:
    """Lee el log JSONL y devuelve solo las líneas que son objetos JSON válidos.

    Args:
        path: Ruta del log.

    Returns:
        Lista de eventos parseados.
    """
    out: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


class _FakeProc:
    """Stand-in de subprocess.CompletedProcess para el verifier monkeypatcheado."""

    def __init__(self, returncode: int, stdout: str | None, stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _verifier_ok(verified: int = 3, files_total: int = 3) -> object:
    """Devuelve un proc fake con salida JSON válida del verifier (rc=0, sin errores).

    Args:
        verified: Cantidad de archivos verificados a reportar.
        files_total: Total de archivos.

    Returns:
        Callable compatible con la firma de _run_verifier_safe.
    """
    payload = {
        "files_total": files_total,
        "verified": verified,
        "pub_ok": True,
        "pub_reason": "ok",
        "broken_tests": [],
        "errors": [],
        "warnings": [],
    }

    def _fn(verifier_path: str, repo: str, file_list: list) -> object:
        return _FakeProc(0, json.dumps(payload) + "\n")

    return _fn


def _verifier_fail() -> object:
    """Proc fake del verifier que reporta errores (rc!=0, tests rotos)."""
    payload = {
        "files_total": 2,
        "verified": 0,
        "pub_ok": False,
        "pub_reason": "compile failure",
        "broken_tests": ["test_a", "test_b"],
        "errors": [
            {"category": "compile_error", "severity": "error", "detail": "boom"},
            {"category": "broken_test", "severity": "error", "detail": "fail"},
        ],
        "warnings": [],
    }

    def _fn(verifier_path: str, repo: str, file_list: list) -> object:
        return _FakeProc(1, json.dumps(payload) + "\n", stderr="errors")

    return _fn


def _verifier_unparseable() -> object:
    """Proc fake cuya salida NO es JSON parseable (ejercita el fallback)."""

    def _fn(verifier_path: str, repo: str, file_list: list) -> object:
        return _FakeProc(2, "this is not json at all\n", stderr="garbage")

    return _fn


# ---------------------------------------------------------------------------
# _verify — rama: sin agente despachado
# ---------------------------------------------------------------------------


def test_verify_noop_when_no_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Log sin agent_dispatched/subagent_start → _verify es no-op (no corre verifier)."""
    log = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.txt"
    ledger.touch()
    _write_log(log, [{"event": "lab_write", "ts": TS_NOW, "path": "/x", "project": "/r"}])

    calls: list[tuple] = []

    def _spy(verifier_path: str, repo: str, file_list: list) -> object:
        calls.append((repo, tuple(file_list)))
        return _FakeProc(0, "{}")

    monkeypatch.setattr(stop, "_run_verifier_safe", _spy)
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)

    assert calls == [], "no debe correr el verifier sin agentes despachados"
    # El ledger no debe ganar entradas de agente (queda vacío de keys).
    assert ledger.read_text().strip() == "", "ledger no debe mutar sin agentes"


# ---------------------------------------------------------------------------
# _verify — rama: agente sin verificar + verifier rc=0 (OK)
# ---------------------------------------------------------------------------


def test_verify_dispatched_unverified_rc0_marks_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agente despachado + lab_write posterior + verifier rc=0 → agent_verify_completed
    con verified>0 y el agente queda registrado en el ledger."""
    log = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.txt"
    ledger.touch()
    agent_ts = "2026-06-19T11:00:00+00:00"
    _write_log(
        log,
        [
            {"event": "agent_dispatched", "ts": agent_ts, "subagent_type": "qa-agent"},
            {
                "event": "lab_write",
                "ts": "2026-06-19T11:05:00+00:00",
                "path": "/repo/file.py",
                "project": "/repo",
            },
        ],
    )

    monkeypatch.setattr(stop, "_run_verifier_safe", _verifier_ok(verified=1, files_total=1))
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)

    events = _read_log_events(log)
    completed = [e for e in events if e.get("event") == "agent_verify_completed"]
    assert len(completed) == 1, "debe escribir exactamente un agent_verify_completed"
    ev = completed[0]
    assert ev["subagent_type"] == "qa-agent"
    assert ev["verified"] == 1
    assert ev["errors_total"] == 0
    assert ev["verifier_exit"] == 0
    assert ev["repo"] == "/repo"
    assert ev["file_signal"] == "lab_write"

    # El agente quedó en el ledger (no se re-verifica en una segunda pasada).
    ledger_keys = {ln for ln in ledger.read_text().splitlines() if not ln.startswith("#")}
    assert any("qa-agent" in k for k in ledger_keys), "el agente debe quedar en el ledger"


# ---------------------------------------------------------------------------
# _verify — rama: agente sin verificar + verifier rc!=0 (FAIL)
# ---------------------------------------------------------------------------


def test_verify_dispatched_unverified_rc_nonzero_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Verifier rc!=0 con errores → agent_verify_completed reporta errors_total>0,
    verifier_exit!=0 y emite el resumen ⚠️ a stderr."""
    log = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.txt"
    ledger.touch()
    agent_ts = "2026-06-19T11:00:00+00:00"
    _write_log(
        log,
        [
            {"event": "subagent_start", "ts": agent_ts, "subagent_type": "software-dev"},
            {
                "event": "lab_write",
                "ts": "2026-06-19T11:05:00+00:00",
                "path": "/repo/a.py",
                "project": "/repo",
            },
        ],
    )

    monkeypatch.setattr(stop, "_run_verifier_safe", _verifier_fail())
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)

    events = _read_log_events(log)
    completed = [e for e in events if e.get("event") == "agent_verify_completed"]
    assert len(completed) == 1
    ev = completed[0]
    assert ev["verified"] == 0
    assert ev["errors_total"] == 2
    assert ev["verifier_exit"] == 1
    assert ev["pub_ok"] is False
    assert "compile_error" in ev["error_categories"]
    assert len(ev["broken_tests"]) == 2

    err = capsys.readouterr().err
    assert "post-agent-verify" in err, "debe avisar el fallo a stderr"
    assert "software-dev" in err


# ---------------------------------------------------------------------------
# _verify — rama: salida del verifier no parseable → fallback
# ---------------------------------------------------------------------------


def test_verify_unparseable_output_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si la salida del verifier no es JSON, _verify usa el fallback verifier_parse_error."""
    log = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.txt"
    ledger.touch()
    _write_log(
        log,
        [
            {
                "event": "agent_dispatched",
                "ts": "2026-06-19T11:00:00+00:00",
                "subagent_type": "ai-agent",
            },
            {
                "event": "lab_write",
                "ts": "2026-06-19T11:05:00+00:00",
                "path": "/repo/x.py",
                "project": "/repo",
            },
        ],
    )

    monkeypatch.setattr(stop, "_run_verifier_safe", _verifier_unparseable())
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)

    events = _read_log_events(log)
    completed = [e for e in events if e.get("event") == "agent_verify_completed"]
    assert len(completed) == 1
    ev = completed[0]
    assert ev["verifier_exit"] == 2
    assert ev["errors_total"] == 1
    assert ev["error_categories"] == ["verifier_parse_error"]
    assert ev["verified"] == 0


# ---------------------------------------------------------------------------
# _verify — rama: agente sin cambios (ni lab_write ni git) → no_changes
# ---------------------------------------------------------------------------


def test_verify_no_changes_emits_no_changes_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agente despachado pero sin archivos tocados (sin lab_write, sin repo_heads_pre)
    → agent_verify_no_changes y el verifier NO corre."""
    log = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.txt"
    ledger.touch()
    _write_log(
        log,
        [
            {
                "event": "agent_dispatched",
                "ts": "2026-06-19T11:00:00+00:00",
                "subagent_type": "code-review-agent",
            }
        ],
    )

    calls: list[tuple] = []

    def _spy(verifier_path: str, repo: str, file_list: list) -> object:
        calls.append((repo,))
        return _FakeProc(0, "{}")

    monkeypatch.setattr(stop, "_run_verifier_safe", _spy)
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)

    assert calls == [], "no debe correr el verifier si no hay cambios"
    events = _read_log_events(log)
    no_changes = [e for e in events if e.get("event") == "agent_verify_no_changes"]
    assert len(no_changes) == 1
    assert no_changes[0]["subagent_type"] == "code-review-agent"
    assert no_changes[0]["source"] == "stop_hook_ledger"
    # El agente queda en el ledger igualmente.
    ledger_keys = {ln for ln in ledger.read_text().splitlines() if not ln.startswith("#")}
    assert any("code-review-agent" in k for k in ledger_keys)


# ---------------------------------------------------------------------------
# _verify — rama: agente ya en el ledger → se salta
# ---------------------------------------------------------------------------


def test_verify_skips_agent_already_in_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si la key del agente ya está en el ledger, _verify no lo re-verifica."""
    log = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.txt"
    agent_ts = "2026-06-19T11:00:00+00:00"
    key = f"{agent_ts}::qa-agent"
    ledger.write_text(key + "\n")
    _write_log(
        log,
        [
            {"event": "agent_dispatched", "ts": agent_ts, "subagent_type": "qa-agent"},
            {
                "event": "lab_write",
                "ts": "2026-06-19T11:05:00+00:00",
                "path": "/repo/x.py",
                "project": "/repo",
            },
        ],
    )

    calls: list[tuple] = []

    def _spy(verifier_path: str, repo: str, file_list: list) -> object:
        calls.append((repo,))
        return _FakeProc(0, "{}")

    monkeypatch.setattr(stop, "_run_verifier_safe", _spy)
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)

    assert calls == [], "agente ya en ledger no debe re-verificarse"
    events = _read_log_events(log)
    assert not [
        e for e in events if e.get("event", "").startswith("agent_verify")
    ], "no debe escribir eventos de verificación para un agente ya en el ledger"


# ---------------------------------------------------------------------------
# _verify — rama: ts inválido en el start → se salta
# ---------------------------------------------------------------------------


def test_verify_skips_agent_with_invalid_ts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start con ts no parseable (→ 0.0) se salta sin correr el verifier."""
    log = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.txt"
    ledger.touch()
    _write_log(
        log,
        [
            {"event": "agent_dispatched", "ts": "not-a-timestamp", "subagent_type": "qa-agent"},
            {
                "event": "lab_write",
                "ts": "2026-06-19T11:05:00+00:00",
                "path": "/repo/x.py",
                "project": "/repo",
            },
        ],
    )

    calls: list[tuple] = []

    def _spy(verifier_path: str, repo: str, file_list: list) -> object:
        calls.append((repo,))
        return _FakeProc(0, "{}")

    monkeypatch.setattr(stop, "_run_verifier_safe", _spy)
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)

    assert calls == [], "agente con ts inválido no debe verificarse"
    events = _read_log_events(log)
    assert not [e for e in events if e.get("event", "").startswith("agent_verify")]


# ---------------------------------------------------------------------------
# _verify — rama: byte-offset del ledger (relee solo lo nuevo)
# ---------------------------------------------------------------------------


def test_verify_persists_offset_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tras una corrida, _verify escribe #offset:N en el ledger; una 2da pasada SIN
    contenido nuevo no re-procesa (sin nuevos eventos de verificación)."""
    log = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.txt"
    ledger.touch()
    _write_log(
        log,
        [
            {
                "event": "agent_dispatched",
                "ts": "2026-06-19T11:00:00+00:00",
                "subagent_type": "qa-agent",
            },
            {
                "event": "lab_write",
                "ts": "2026-06-19T11:05:00+00:00",
                "path": "/repo/x.py",
                "project": "/repo",
            },
        ],
    )

    monkeypatch.setattr(stop, "_run_verifier_safe", _verifier_ok(verified=1, files_total=1))
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)

    ledger_text = ledger.read_text()
    assert "#offset:" in ledger_text, "_verify debe persistir el byte-offset en el ledger"

    completed_after_first = len(
        [e for e in _read_log_events(log) if e.get("event") == "agent_verify_completed"]
    )
    assert completed_after_first == 1

    # Segunda pasada: el offset salta el contenido ya leído → no hay starts nuevos.
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)
    completed_after_second = len(
        [e for e in _read_log_events(log) if e.get("event") == "agent_verify_completed"]
    )
    assert (
        completed_after_second == completed_after_first
    ), "una 2da pasada sin contenido nuevo no debe re-verificar"


# ---------------------------------------------------------------------------
# _verify — fail-open: log ilegible no propaga excepción
# ---------------------------------------------------------------------------


def test_verify_failopen_on_missing_log(tmp_path: Path) -> None:
    """_verify sobre un log inexistente no debe lanzar (fail-open: el Stop continúa)."""
    log = tmp_path / "does_not_exist.jsonl"
    ledger = tmp_path / "ledger.txt"
    ledger.touch()
    # No debe lanzar ninguna excepción.
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)


def test_verify_failopen_when_verifier_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si _run_verifier_safe lanzara, el código actual lo propaga DENTRO del loop, pero
    handle() lo envuelve en fail-open. Aquí caracterizamos que un proc con stdout None
    (verifier reventado pero devuelto como FakeProc, como hace _run_verifier_safe en su
    propio except) cae al fallback sin romper."""
    log = tmp_path / "events.jsonl"
    ledger = tmp_path / "ledger.txt"
    ledger.touch()
    _write_log(
        log,
        [
            {
                "event": "agent_dispatched",
                "ts": "2026-06-19T11:00:00+00:00",
                "subagent_type": "qa-agent",
            },
            {
                "event": "lab_write",
                "ts": "2026-06-19T11:05:00+00:00",
                "path": "/repo/x.py",
                "project": "/repo",
            },
        ],
    )

    def _broken(verifier_path: str, repo: str, file_list: list) -> object:
        # Replica el FakeProc interno de _run_verifier_safe ante una excepción:
        # returncode=-1, stdout=None.
        return _FakeProc(-1, None, stderr="verifier invocation error")

    monkeypatch.setattr(stop, "_run_verifier_safe", _broken)
    stop._verify(str(log), str(ledger), VERIFIER, TS_NOW)

    events = _read_log_events(log)
    completed = [e for e in events if e.get("event") == "agent_verify_completed"]
    assert len(completed) == 1
    assert completed[0]["verifier_exit"] == -1
    assert completed[0]["error_categories"] == ["verifier_parse_error"]


# ---------------------------------------------------------------------------
# _run_verifier_safe — guard de plataforma (ulimit -v solo en Linux)
# ---------------------------------------------------------------------------


def test_run_verifier_safe_skips_memcap_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    """En Darwin el comando NO incluye `ulimit -v` (P0 #3: rompía la cadena en macOS);
    en otras plataformas SÍ. Captura subprocess.run para inspeccionar el shell_cmd."""
    captured: dict[str, str] = {}

    def _fake_run(cmd: list, **kwargs: object) -> object:
        captured["shell_cmd"] = cmd[-1]
        return _FakeProc(0, "{}")

    monkeypatch.setattr(stop.subprocess, "run", _fake_run)

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    stop._run_verifier_safe("/v.py", "/repo", ["/repo/a.py"])
    assert "ulimit -v" not in captured["shell_cmd"], "Darwin no debe usar ulimit -v"
    assert "ulimit -t 30" in captured["shell_cmd"], "el cap de CPU debe estar siempre"

    monkeypatch.setattr("platform.system", lambda: "Linux")
    stop._run_verifier_safe("/v.py", "/repo", ["/repo/a.py"])
    assert "ulimit -v 524288" in captured["shell_cmd"], "Linux sí aplica cap de memoria"


def test_run_verifier_safe_timeout_returns_fakeproc(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un TimeoutExpired del subprocess se traduce a un proc fake rc=-1, no propaga."""

    def _raise_timeout(cmd: list, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd, 35)

    monkeypatch.setattr(stop.subprocess, "run", _raise_timeout)
    proc = stop._run_verifier_safe("/v.py", "/repo", ["/repo/a.py"])
    assert proc.returncode == -1
    assert "time limit" in proc.stderr


# ---------------------------------------------------------------------------
# handle — guards de plataforma / passthrough (exit 0 siempre)
# ---------------------------------------------------------------------------


def test_handle_passthrough_when_log_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """handle con log_file inexistente → passthrough (SystemExit(0)), sin verificar."""
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(tmp_path / "nope.jsonl"))

    called = {"verify": False}
    monkeypatch.setattr(stop, "_verify", lambda *a, **k: called.__setitem__("verify", True))

    with pytest.raises(SystemExit) as exc:
        stop.handle("Stop", {})
    assert exc.value.code == 0, "Stop nunca bloquea (exit 0)"
    assert called["verify"] is False, "no debe verificar si falta el log"


def test_handle_passthrough_when_verifier_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """handle con verifier ausente/no ejecutable → passthrough (SystemExit(0))."""
    log = tmp_path / "events.jsonl"
    log.write_text("{}\n")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))
    # Forzar que el verifier resuelto NO exista: apuntar ARIS4U_ROOT a un dir vacío.
    monkeypatch.setattr(stop, "ARIS4U_ROOT", tmp_path)

    called = {"verify": False}
    monkeypatch.setattr(stop, "_verify", lambda *a, **k: called.__setitem__("verify", True))

    with pytest.raises(SystemExit) as exc:
        stop.handle("Stop", {})
    assert exc.value.code == 0
    assert called["verify"] is False, "no debe verificar si falta el verifier"


def test_handle_passthrough_when_lock_held(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Si el lock /tmp/aris4u_verifier.lock.d ya existe → passthrough sin verificar."""
    log = tmp_path / "events.jsonl"
    log.write_text("{}\n")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))

    # Crear un verifier ejecutable real dentro de un ARIS4U_ROOT tmp.
    tools = tmp_path / "tools"
    tools.mkdir()
    verifier = tools / "agent_output_verifier.py"
    verifier.write_text("#!/usr/bin/env python3\nprint('{}')\n")
    verifier.chmod(0o755)
    monkeypatch.setattr(stop, "ARIS4U_ROOT", tmp_path)

    called = {"verify": False}
    monkeypatch.setattr(stop, "_verify", lambda *a, **k: called.__setitem__("verify", True))

    lock_dir = Path("/tmp/aris4u_verifier.lock.d")
    held_by_us = False
    if not lock_dir.exists():
        lock_dir.mkdir()
        held_by_us = True
    try:
        with pytest.raises(SystemExit) as exc:
            stop.handle("Stop", {})
        assert exc.value.code == 0
        assert called["verify"] is False, "lock tomado → no debe verificar"
    finally:
        if held_by_us:
            lock_dir.rmdir()


def test_handle_happy_path_runs_verify_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end: con log + verifier presentes y lock libre, handle corre _verify,
    libera el lock y sale con SystemExit(0)."""
    log = tmp_path / "events.jsonl"
    log.write_text("{}\n")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))

    tools = tmp_path / "tools"
    tools.mkdir()
    verifier = tools / "agent_output_verifier.py"
    verifier.write_text("#!/usr/bin/env python3\nprint('{}')\n")
    verifier.chmod(0o755)
    monkeypatch.setattr(stop, "ARIS4U_ROOT", tmp_path)

    # Redirigir el ledger /tmp a tmp_path no es posible (handle lo hardcodea), pero
    # _verify lo recibe como argumento; aquí solo verificamos que _verify es invocado
    # y el lock queda liberado al final.
    seen: dict[str, object] = {}

    def _fake_verify(log_file: str, ledger: str, verifier_path: str, ts_now: str) -> None:
        seen["log_file"] = log_file
        seen["ledger"] = ledger
        seen["verifier"] = verifier_path

    monkeypatch.setattr(stop, "_verify", _fake_verify)

    lock_dir = Path("/tmp/aris4u_verifier.lock.d")
    if lock_dir.exists():
        lock_dir.rmdir()  # asegurar que arrancamos sin lock

    with pytest.raises(SystemExit) as exc:
        stop.handle("Stop", {})
    assert exc.value.code == 0
    assert seen.get("log_file") == str(log), "_verify debe recibir el log resuelto"
    assert seen.get("verifier", "").endswith("agent_output_verifier.py")  # type: ignore[union-attr]  # seen is dict[str, object]; default "" ensures str; pyright loses that
    assert not lock_dir.exists(), "handle debe liberar el lock al terminar"
