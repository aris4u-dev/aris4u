"""Cobertura extra de DOS handlers PreToolUse advisory (shadow-mode, fail-open):

  - hooks/dispatch/handlers/f5_prevalidation.py
        Gate de calidad de output F5 en Write|Edit. SIEMPRE advisory: si el verdict
        del motor F5 != PASS, emite un aviso; nunca bloquea. Fail-open total: si el
        motor crashea o no aplica → PASS sin ruido. Selección de contrato por extensión.
  - hooks/dispatch/handlers/phi_sanitizer.py
        Detecta PHI tier-1 (SSN/DOB/MRN/NPI) en Bash|Write|Edit|Read, SOLO en contexto
        healthcare. Advisory (nunca bloquea). Fuera de healthcare = no-op. Audita a log.

Dos planos de prueba (mismo patrón que el resto de tests/dispatch):

  A) UNIT — import directo del handler como función pura `(inp) -> Verdict`. Más
     preciso para los bordes (cada contrato, cada patrón PHI, healthcare on/off).
     `ARIS4U_ROOT` se monkeypatchea a tmp_path en CADA módulo que escribe al log
     (_log / _log_audit) → NUNCA se toca el logs/v16.1-events.jsonl REAL (Regla #2).
  B) INTEGRACIÓN — vía tests/dispatch/_invoke.py (subproceso, stdin JSON), igual que
     test_pre_tool_use.py. Verifica el contrato de salida del dispatcher: advisory =
     additionalContext exit 0; shadow/no-op = exit 0 sin salida; jamás exit 2.

El motor F5 (`engine.v16.f5_validacion.ValidacionEngine`) es determinista para estas
entradas (tier1 contrato + tier2 heurística textual Phase 1, SIN red/Ollama).

Corre:  .venv312/bin/python3 -m pytest tests/dispatch/test_pre_handlers_extra.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
INVOKE = str(ROOT / "tests" / "dispatch" / "_invoke.py")
HOOKS = ROOT / "hooks"

# Los handlers viven bajo hooks/dispatch/handlers/. Para importarlos directo (plano A)
# necesitamos hooks/ en sys.path, igual que hace _invoke.py para el subproceso.
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from dispatch.handlers import f5_prevalidation as f5  # noqa: E402
from dispatch.handlers import phi_sanitizer as sanitizer  # noqa: E402
from dispatch.handlers import verdict as V  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures de aislamiento — Regla #2: NUNCA escribir al log REAL.
# phi_sanitizer._log y phi_guard._log_audit escriben a ARIS4U_ROOT/logs/...
# (NO honran ARIS4U_EVENTS_LOG). Redirigimos ARIS4U_ROOT a tmp_path en CADA
# módulo que pueda loguear, y creamos logs/ ahí para ejercitar el path real.
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_log_root(tmp_path, monkeypatch):
    """Redirige ARIS4U_ROOT (en sanitizer + phi_guard) a un tmp con logs/ creado.

    Devuelve la ruta del log temporal donde caerían los eventos phi_detected.
    """
    from dispatch.handlers import phi_guard as phi_guard
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(sanitizer, "ARIS4U_ROOT", tmp_path)
    monkeypatch.setattr(phi_guard, "ARIS4U_ROOT", tmp_path)
    return tmp_path / "logs" / "v16.1-events.jsonl"


@pytest.fixture
def clean_healthcare_env(monkeypatch):
    """Borra toda señal de healthcare por env (tests no-healthcare deterministas)."""
    monkeypatch.delenv("ARIS4U_HEALTHCARE", raising=False)
    monkeypatch.delenv("ARIS4U_CLIENT", raising=False)
    # CI hermeticity: _HEALTHCARE_PATH_MARKERS is computed at import from config.
    # Empty without ~/.aris4u/config.json → inject canonical client-c markers so
    # test_phi_sanitizer_activates_inside_client_cwd passes in CI.
    monkeypatch.setattr(sanitizer._pg, "_HEALTHCARE_PATH_MARKERS", ("client-c", "/client-c/"))


# ===========================================================================
# PLANO A — f5_prevalidation.check (UNIT, función pura)
# ===========================================================================


def test_f5_non_write_edit_is_pass():
    """Tool que no es Write|Edit → PASS inmediato (no aplica el gate)."""
    assert f5.check("Bash", {"command": "ls"}).kind == V.PASS
    assert f5.check("Read", {"file_path": "/tmp/x.py"}).kind == V.PASS


def test_f5_empty_content_is_pass():
    """Write/Edit sin contenido → PASS (nada que validar)."""
    assert f5.check("Write", {"file_path": "/tmp/a.py"}).kind == V.PASS
    assert f5.check("Edit", {"file_path": "/tmp/a.py", "content": ""}).kind == V.PASS
    assert f5.check("Write", {}).kind == V.PASS


def test_f5_clean_code_passes():
    """Código .py válido y suficientemente largo → verdict PASS → advisory vacío (PASS)."""
    content = (
        'def add(a: int, b: int) -> int:\n'
        '    """Suma dos enteros.\n\n    Returns:\n        La suma.\n    """\n'
        '    return a + b\n'
    )
    v = f5.check("Write", {"file_path": "/tmp/good.py", "content": content})
    assert v.kind == V.PASS, f"código limpio no debe avisar: {v.text}"


def test_f5_short_code_advises_not_blocks():
    """Código demasiado corto (< min_length 20 del contrato 'code') → ADVISE, jamás BLOCK."""
    v = f5.check("Write", {"file_path": "/tmp/tiny.py", "content": "x"})
    assert v.kind == V.ADVISE, f"esperaba advisory, fue {v.kind}"
    assert v.kind != V.BLOCK and v.kind != V.DENY, "shadow-mode: NUNCA bloquea"
    assert "F5.VALIDACION" in v.text
    # El mensaje arrastra el verdict del motor y la primera issue.
    assert "FAIL" in v.text or "UNCERTAIN" in v.text


def test_f5_advisory_carries_first_issue():
    """El advisory incluye la descripción de la primera issue del motor F5."""
    v = f5.check("Write", {"file_path": "/tmp/tiny.py", "content": "x"})
    assert v.kind == V.ADVISE
    # tier1 contract reporta 'Output too short' como primera issue para 'x' en 'code'.
    assert "too short" in v.text.lower()


def test_f5_failopen_when_engine_raises(monkeypatch):
    """Si el motor F5 crashea (infra rota) → PASS (shadow-mode, fail-open total)."""
    import engine.v16.f5_validacion as f5mod

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("engine exploded")

    monkeypatch.setattr(f5mod, "ValidacionEngine", _Boom)
    v = f5.check("Write", {"file_path": "/tmp/tiny.py", "content": "x"})
    assert v.kind == V.PASS, "motor roto debe degradar a PASS, no avisar ni romper"


def test_f5_failopen_when_validate_raises(monkeypatch):
    """Si validate() lanza durante la corrida → PASS (no propaga la excepción)."""
    import engine.v16.f5_validacion as f5mod

    class _Engine:
        def __init__(self, *a, **k):
            pass

        def validate(self, *a, **k):
            raise ValueError("boom mid-validate")

    monkeypatch.setattr(f5mod, "ValidacionEngine", _Engine)
    v = f5.check("Edit", {"file_path": "/tmp/tiny.py", "content": "x"})
    assert v.kind == V.PASS


def test_f5_contract_selection_by_extension():
    """_contract_for replica el `case` del .sh: formato + min_length por tipo."""
    assert f5._contract_for("/tmp/foo.py") == ("code", 20)
    assert f5._contract_for("/tmp/foo.md") == ("docs", 10)
    assert f5._contract_for("/tmp/foo.json") == ("config", 2)
    assert f5._contract_for("/tmp/foo.yaml") == ("config", 2)
    assert f5._contract_for("/tmp/deploy.sh") == ("script", 10)
    assert f5._contract_for("/tmp/Widget.tsx") == ("code", 20)
    # Marcadores de test → contrato 'test' (umbral relajado a 5).
    assert f5._contract_for("/repo/tests/test_x.py") == ("test", 5)
    assert f5._contract_for("test_module.py") == ("test", 5)
    assert f5._contract_for("/repo/foo.spec.ts") == ("test", 5)
    # Sin extensión conocida → fallback ('code', 5).
    assert f5._contract_for("/tmp/Makefile") == ("code", 5)


def test_f5_returns_verdict_instance():
    """check() siempre devuelve un Verdict (contrato del orquestador)."""
    v = f5.check("Write", {"file_path": "/tmp/tiny.py", "content": "x"})
    assert isinstance(v, V.Verdict)


# ===========================================================================
# PLANO A — phi_sanitizer.check (UNIT, función pura)
# ===========================================================================


def test_phi_sanitizer_non_target_tool_is_pass():
    """Tool fuera de {Bash,Write,Edit,Read} → PASS (no aplica)."""
    v = sanitizer.check("WebFetch", {"prompt": "patient SSN 123-45-6789"}, "/tmp")
    assert v.kind == V.PASS


def test_phi_sanitizer_empty_text_is_pass():
    """Sin texto en tool_input → PASS."""
    assert sanitizer.check("Bash", {}, "/tmp").kind == V.PASS
    assert sanitizer.check("Write", {"file_path": ""}, "/tmp").kind == V.PASS


def test_phi_sanitizer_nonhealthcare_is_noop(clean_healthcare_env):
    """PHI presente pero FUERA de healthcare → PASS (no-op, sin aviso ni log)."""
    text = "patient SSN 123-45-6789 dob 01-15-1990"
    v = sanitizer.check("Bash", {"command": f"curl api.example.com -d '{text}'"}, "/tmp")
    assert v.kind == V.PASS, "fuera de healthcare NO debe avisar"


def test_phi_sanitizer_healthcare_no_phi_is_pass(monkeypatch, clean_healthcare_env):
    """En healthcare pero SIN PHI tier-1 → PASS (no hay nada que avisar)."""
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    v = sanitizer.check("Bash", {"command": "echo hola mundo"}, "/tmp")
    assert v.kind == V.PASS


@pytest.mark.parametrize(
    "text, expected_pattern",
    [
        ("patient ssn 123-45-6789", "SSN"),
        ("date 01-15-1990 of admission", "DOB"),
        ("mrn: 1234567 chart", "MRN"),
        ("provider npi 1234567890 billed", "NPI"),
    ],
)
def test_phi_sanitizer_healthcare_advises_each_tier1(
    text, expected_pattern, monkeypatch, clean_healthcare_env, isolated_log_root
):
    """En healthcare, cada patrón tier-1 (SSN/DOB/MRN/NPI) → ADVISE, jamás BLOCK."""
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    v = sanitizer.check("Bash", {"command": f"send {text}"}, "/tmp")
    assert v.kind == V.ADVISE, f"esperaba advisory para {expected_pattern}, fue {v.kind}"
    assert v.kind not in (V.BLOCK, V.DENY), "phi_sanitizer es advisory, NUNCA bloquea"
    assert "[PHI-SANITIZER]" in v.text
    assert expected_pattern in v.text
    # Verifica el side-effect de auditoría en el log AISLADO (no el real).
    assert isolated_log_root.is_file(), "debe registrar el evento phi_detected"
    line = isolated_log_root.read_text().strip().splitlines()[-1]
    event = json.loads(line)
    assert event["event"] == "phi_detected"
    assert event["pattern"] == expected_pattern
    assert event["hook"] == "phi_sanitizer"
    # El log NUNCA contiene el PHI en claro (solo la etiqueta del patrón).
    assert "123-45-6789" not in line


def test_phi_sanitizer_detection_precedence(monkeypatch, clean_healthcare_env):
    """_detect respeta la precedencia SSN > DOB > MRN > NPI del .sh."""
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    # SSN y DOB ambos presentes → gana SSN.
    assert sanitizer._detect("ssn 123-45-6789 dob 01-15-1990") == "SSN"
    # Solo DOB.
    assert sanitizer._detect("nacio 12-31-2005") == "DOB"
    # Solo MRN.
    assert sanitizer._detect("mrn-987654") == "MRN"
    # Nada → cadena vacía.
    assert sanitizer._detect("just a normal sentence") == ""


def test_phi_sanitizer_client_env_no_longer_activates(monkeypatch, clean_healthcare_env, isolated_log_root):
    """ARIS4U_CLIENT ya NO activa PHI (off-by-default 2026-06-22).

    La activación implícita por env de cliente / bridge stale / texto se eliminó
    (causaba falsos positivos). PHI solo se enciende explícito: ARIS4U_HEALTHCARE=1,
    marker .aris-healthcare en cwd, o cwd dentro de un proyecto cliente healthcare.
    """
    monkeypatch.delenv("ARIS4U_HEALTHCARE", raising=False)
    monkeypatch.setenv("ARIS4U_CLIENT", "client-c")
    v = sanitizer.check("Write", {"file_path": "/tmp/x.txt", "content": "ssn 123-45-6789"}, "/tmp")
    assert v.kind == V.PASS  # cwd no-médico + sin switch → PHI OFF (era el falso positivo)


def test_phi_sanitizer_activates_inside_client_cwd(monkeypatch, clean_healthcare_env, isolated_log_root):
    """cwd DENTRO de un proyecto cliente healthcare SÍ activa (red de seguridad)."""
    monkeypatch.delenv("ARIS4U_HEALTHCARE", raising=False)
    monkeypatch.delenv("ARIS4U_CLIENT", raising=False)
    v = sanitizer.check(
        "Write",
        {"file_path": "x.txt", "content": "ssn 123-45-6789"},
        "/Users/x/projects/client-c/inventory-system",
    )
    assert v.kind == V.ADVISE
    assert "SSN" in v.text


def test_phi_sanitizer_log_missing_dir_is_safe(monkeypatch, clean_healthcare_env, tmp_path):
    """Si logs/ NO existe, _log no escribe ni crashea (fail-safe) y check sigue ADVISE."""
    from dispatch.handlers import phi_guard as phi_guard
    # ARIS4U_ROOT a un tmp SIN crear logs/ → _log debe ser no-op silencioso.
    monkeypatch.setattr(sanitizer, "ARIS4U_ROOT", tmp_path)
    monkeypatch.setattr(phi_guard, "ARIS4U_ROOT", tmp_path)
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    v = sanitizer.check("Bash", {"command": "ssn 123-45-6789"}, "/tmp")
    assert v.kind == V.ADVISE, "el aviso se emite aunque el log no pueda escribir"
    assert not (tmp_path / "logs").exists(), "sin logs/ no debe crearse nada"


def test_phi_sanitizer_returns_verdict_instance(monkeypatch, clean_healthcare_env, isolated_log_root):
    """check() siempre devuelve un Verdict."""
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    v = sanitizer.check("Bash", {"command": "ssn 123-45-6789"}, "/tmp")
    assert isinstance(v, V.Verdict)


# ===========================================================================
# PLANO B — INTEGRACIÓN vía dispatcher (_invoke.py, subproceso, stdin JSON)
# Verifica el contrato de SALIDA: advisory = additionalContext exit 0;
# shadow/no-op = exit 0 sin salida; en NINGÚN caso exit 2 (no son bloqueantes).
# ===========================================================================


def _isolated_root(tmp_path) -> Path:
    """Crea un CLAUDE_PLUGIN_ROOT temporal: symlink a engine/, SIN logs/.

    El subproceso del dispatcher resuelve ARIS4U_ROOT desde CLAUDE_PLUGIN_ROOT
    (ver dispatch/contract.py). Apuntándolo a este tmp:
      - `engine/` (symlink) → el motor F5 sigue siendo importable (advisory funciona).
      - SIN `logs/` → phi_sanitizer._log / phi_guard._log_audit son no-ops silenciosos
        (`if not log_file.parent.is_dir(): return`). NUNCA se toca el log REAL (Regla #2).
    """
    root = tmp_path / "iso_root"
    root.mkdir(exist_ok=True)
    link = root / "engine"
    if not link.exists():
        link.symlink_to(ROOT / "engine")
    return root


def _run_dispatch(payload: str, tmp_path, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Corre el orquestador PreToolUse completo vía _invoke (igual que test_pre_tool_use).

    Aísla el log del subproceso vía CLAUDE_PLUGIN_ROOT → tmp sin logs/ (Regla #2).
    """
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(_isolated_root(tmp_path))}
    if env_extra is not None:
        for k, v in env_extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run(
        [PY, INVOKE, "pre_tool_use", "PreToolUse"],
        input=payload, capture_output=True, text=True, timeout=60, env=env,
    )


def _payload(tool_name: str, tool_input: dict, cwd: str | None = None) -> str:
    d: dict = {"tool_name": tool_name, "tool_input": tool_input}
    if cwd is not None:
        d["cwd"] = cwd
    return json.dumps(d)


def test_dispatch_f5_short_code_is_advisory_not_block(tmp_path):
    """Write de .py corto → el dispatcher emite additionalContext F5 (exit 0), NO exit 2."""
    payload = _payload("Write", {"file_path": str(tmp_path / "tiny.py"), "content": "x"})
    # Sin healthcare para que f5 sea el único advisory relevante; limpiamos señales PHI.
    proc = _run_dispatch(payload, tmp_path, env_extra={"ARIS4U_HEALTHCARE": None, "ARIS4U_CLIENT": None})
    assert proc.returncode == 0, f"shadow-mode: jamás exit 2 (fue {proc.returncode})\n{proc.stderr}"
    assert proc.stdout.strip(), "esperaba additionalContext"
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "F5.VALIDACION" in ctx


def test_dispatch_f5_clean_code_is_noop(tmp_path):
    """Write de .py limpio → no-op (exit 0, sin salida)."""
    content = (
        'def f(x: int) -> int:\n'
        '    """Doc.\n\n    Returns:\n        x.\n    """\n'
        '    return x\n'
    )
    payload = _payload("Write", {"file_path": str(tmp_path / "ok.py"), "content": content})
    proc = _run_dispatch(payload, tmp_path, env_extra={"ARIS4U_HEALTHCARE": None, "ARIS4U_CLIENT": None})
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", f"código limpio → sin salida, hubo: {proc.stdout!r}"


def test_dispatch_phi_sanitizer_advisory_in_healthcare(tmp_path):
    """PHI tier-1 en Read + healthcare → additionalContext PHI-SANITIZER (exit 0), NO bloquea.

    Se usa Read (NO Bash/WebFetch): así el BLOQUEANTE phi_guard (que solo mira
    Bash|WebFetch|WebSearch) NO entra y aislamos el advisory de phi_sanitizer.
    """
    payload = _payload("Read", {"file_path": "/tmp/chart_ssn_123-45-6789.txt"})
    proc = _run_dispatch(payload, tmp_path, env_extra={"ARIS4U_HEALTHCARE": "1"})
    assert proc.returncode == 0, f"advisory no bloquea (fue {proc.returncode})\n{proc.stderr}"
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "[PHI-SANITIZER]" in ctx
    assert "SSN" in ctx


def test_dispatch_phi_sanitizer_noop_outside_healthcare(tmp_path):
    """Mismo PHI en Read pero FUERA de healthcare → no-op (exit 0, sin salida)."""
    payload = _payload("Read", {"file_path": "/tmp/chart_ssn_123-45-6789.txt"}, "/tmp")
    proc = _run_dispatch(payload, tmp_path, env_extra={"ARIS4U_HEALTHCARE": None, "ARIS4U_CLIENT": None})
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", "fuera de healthcare → no-op, sin salida"


# ---------------------------------------------------------------------------
# WS-G(b) — un guard BLOQUEANTE que se degrada a fail-open NO debe ser MUDO.
# ---------------------------------------------------------------------------


def test_blocker_crash_failopen_leaves_trace(tmp_path, monkeypatch, capsys):
    """Si migration_linter (bloqueante) crashea: la cadena sigue (exit 0, NO exit 2)
    pero deja rastro en stderr y en el event log (guard_degraded_failopen)."""
    from dispatch.events import pre_tool_use as pt
    from dispatch import contract

    # Aislar el event log a tmp (Regla #2: nunca el log real).
    monkeypatch.setattr(contract, "ARIS4U_ROOT", tmp_path)
    (tmp_path / "logs").mkdir()

    def _boom(*_a, **_k):
        raise RuntimeError("linter explotó")

    monkeypatch.setattr(pt._migration, "check", _boom)

    inp = {
        "tool_name": "Bash",
        "tool_input": {"command": "supabase db push"},
        "cwd": str(tmp_path),
    }
    with pytest.raises(SystemExit) as se:
        pt.handle("PreToolUse", inp)

    # Fail-open: NO bloquea (exit 0, jamás exit 2 por el crash del guard).
    assert se.value.code == 0

    # Rastro visible en stderr.
    err = capsys.readouterr().err
    assert "migration_linter" in err and "DEGRAD" in err.upper()

    # Rastro persistente en el event log.
    log = tmp_path / "logs" / "v16.1-events.jsonl"
    assert log.exists(), "el guard degradado debe dejar evento en v16.1-events.jsonl"
    body = log.read_text()
    assert "guard_degraded_failopen" in body and "migration_linter" in body


def test_advisory_crash_stays_silent(tmp_path, monkeypatch, capsys):
    """Un handler NO bloqueante que crashea sigue siendo mudo (no es de seguridad)."""
    from dispatch.events import pre_tool_use as pt
    from dispatch import contract

    monkeypatch.setattr(contract, "ARIS4U_ROOT", tmp_path)
    (tmp_path / "logs").mkdir()

    def _boom(*_a, **_k):
        raise RuntimeError("type_hints explotó")

    monkeypatch.setattr(pt._guards, "type_hints", _boom)

    inp = {
        "tool_name": "Write",
        "tool_input": {"file_path": "/tmp/x.py", "content": "x=1\n"},
        "cwd": str(tmp_path),
    }
    with pytest.raises(SystemExit) as se:
        pt.handle("PreToolUse", inp)
    assert se.value.code == 0
    err = capsys.readouterr().err
    assert "type_hints" not in err  # advisory degradado = mudo (solo los bloqueantes hablan)
    log = tmp_path / "logs" / "v16.1-events.jsonl"
    assert not (log.exists() and "guard_degraded_failopen" in log.read_text())


if __name__ == "__main__":
    sys.exit(subprocess.call([PY, "-m", "pytest", __file__, "-v"]))
