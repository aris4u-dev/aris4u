"""Tests de CARACTERIZACIÓN para session_end._write_digest y _run_analyzer_throttled.

Fijan el comportamiento ACTUAL de ambas funciones (`hooks/dispatch/events/
session_end.py`, CC=26 y CC=22 sin tests previos) como RED DE SEGURIDAD antes de
un refactor que preserva comportamiento. Los asserts describen lo que el código
hace HOY; deben PASAR contra el código tal cual está hoy.

Patrón (idéntico a test_subagent_start / test_stop): in-process con mocks.

  - Los imports de `engine.v16.*` son LAZY dentro de las funciones (`from
    engine.v16.session_manager import save_digest, ...`), así que se monkeypatchean
    en el MÓDULO `engine.v16.session_manager` / `engine.v16.config` ANTES de invocar
    (el bind ocurre en runtime). save_digest se reemplaza por un capturador que
    guarda kwargs → assert del contenido exacto del digest, sin tocar la DB real.
  - La sub-consulta a SESSIONS_DB (decisiones/guards recientes) se redirige a una DB
    sqlite temporal monkeypatcheando `engine.v16.config.SESSIONS_DB`.
  - El enriquecimiento narrativo local se desactiva con ARIS4U_DIGEST_NARRATIVE=0
    salvo en el test que lo ejercita, para aislar el summary factual determinista.
  - claude-mem.db se neutraliza apuntando HOME a tmp (el path no existe → rama saltada).
  - El orquestador F7 y token_utils fallan al import (no instalados/mockeados) → ramas
    fail-open ejercitadas por defecto.
  - _run_analyzer_throttled recibe TODO su estado vía env (ARIS4U_ANALYZE_STATE_FILE,
    ARIS4U_ANALYZE_THROTTLE_SECS, ARIS4U_VALIDATION_LOG, ARIS4U_LOG_FILE) → se redirige
    a tmp_path para no tocar /tmp ni el log real.

Corre:
    .venv312/bin/python3 -m pytest tests/dispatch/test_session_end_digest.py -q -p no:cacheprovider
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dispatch.events import session_end as se  # noqa: E402
from engine.v16 import config as v16_config  # noqa: E402
from engine.v16 import session_manager as sm  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_digest(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Captura los kwargs pasados a save_digest sin escribir en la DB real.

    Returns:
        Dict poblado con los kwargs del último save_digest (vacío si no se llamó).
    """
    sink: dict = {}

    def _capture(**kwargs: object) -> None:
        sink.clear()
        sink.update(kwargs)

    monkeypatch.setattr(sm, "save_digest", _capture)
    return sink


@pytest.fixture
def stub_engine(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stubs deterministas para get_stats/get_all_guards/init_db.

    Returns:
        Dict mutable con 'stats' y 'guards' que el test puede ajustar antes de invocar.
    """
    state = {
        "stats": {"decisions": 5, "guards": 3},
        "guards": [
            {"severity": "critical", "pattern": "no-latest-docker"},
            {"severity": "warning", "pattern": "minor"},
        ],
        "init_db_called": False,
    }

    def _init_db() -> None:
        state["init_db_called"] = True

    monkeypatch.setattr(sm, "init_db", _init_db)
    monkeypatch.setattr(sm, "get_stats", lambda: state["stats"])
    monkeypatch.setattr(sm, "get_all_guards", lambda: state["guards"])
    return state


@pytest.fixture
def recent_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Crea una sessions.db temporal con esquema mínimo y la cablea en config.

    Tablas `decisions` (decision, domain, created_at) y `guards` (pattern,
    severity, created_at). Redirige `engine.v16.config.SESSIONS_DB` a ella.

    Returns:
        Path de la DB temporal (vacía de filas por defecto).
    """
    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE decisions (decision TEXT, domain TEXT, created_at TEXT, trust_source TEXT DEFAULT 'user')"
    )
    conn.execute(
        "CREATE TABLE guards (pattern TEXT, severity TEXT, created_at TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(v16_config, "SESSIONS_DB", db_path)
    return db_path


@pytest.fixture
def neutralize_side_effects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Aísla claude-mem y narrativa: HOME a tmp (sin claude-mem.db) + narrativa off."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARIS4U_DIGEST_NARRATIVE", "0")
    # Sin telemetría salvo que un test la pida.
    monkeypatch.delenv("ARIS4U_VALIDATION_LOG", raising=False)


# ===========================================================================
# _write_digest
# ===========================================================================

def test_write_digest_builds_summary_and_calls_save(
    captured_digest: dict,
    stub_engine: dict,
    recent_db: Path,
    neutralize_side_effects: None,
) -> None:
    """Happy path: summary factual armado + save_digest recibe los kwargs correctos.

    Caracteriza: formato del summary (DB stats + critical guards), tags='v16',
    digest_id==session_id==session_id, built='No commits' sin commits.
    """
    se._write_digest(ROOT, "2026-06-19_deadbeef")

    assert stub_engine["init_db_called"] is True, "init_db debe correr"
    assert captured_digest, "save_digest debe ser invocado"
    assert captured_digest["digest_id"] == "2026-06-19_deadbeef"
    assert captured_digest["session_id"] == "2026-06-19_deadbeef"
    assert captured_digest["tags"] == "v16"
    # 5 decisions, 3 guards + 1 critical guard ('no-latest-docker').
    assert "DB: 5 decisions, 3 guards" in captured_digest["summary"]
    assert "Critical guards: 1" in captured_digest["summary"]
    assert captured_digest["summary"].endswith(".")
    # Sin filas recientes en la DB → decisions/guards vacíos.
    assert captured_digest["decisions"] == ""
    assert captured_digest["guards"] == ""


def test_write_digest_includes_recent_decisions_and_guards(
    captured_digest: dict,
    stub_engine: dict,
    recent_db: Path,
    neutralize_side_effects: None,
) -> None:
    """Con filas recientes (<8h) en la DB, decisions_text/guards_text se formatean."""
    conn = sqlite3.connect(str(recent_db))
    conn.execute(
        "INSERT INTO decisions (decision, domain, created_at) VALUES (?, ?, datetime('now'))",
        ("Use Flyway for all migrations", "database"),
    )
    conn.execute(
        "INSERT INTO guards (pattern, severity, created_at) VALUES (?, ?, datetime('now'))",
        ("no-bare-except", "critical"),
    )
    conn.commit()
    conn.close()

    se._write_digest(ROOT, "sess-recent")

    assert captured_digest["decisions"] == "[database] Use Flyway for all migrations"
    assert captured_digest["guards"] == "[critical] no-bare-except"


def test_write_digest_excludes_old_rows(
    captured_digest: dict,
    stub_engine: dict,
    recent_db: Path,
    neutralize_side_effects: None,
) -> None:
    """Filas con created_at > 8h NO entran al digest (filtro temporal)."""
    conn = sqlite3.connect(str(recent_db))
    conn.execute(
        "INSERT INTO decisions (decision, domain, created_at) "
        "VALUES (?, ?, datetime('now', '-10 hours'))",
        ("ancient decision", "old"),
    )
    conn.commit()
    conn.close()

    se._write_digest(ROOT, "sess-old")
    assert captured_digest["decisions"] == "", "decisiones de hace 10h no deben aparecer"


def test_write_digest_no_critical_guards_omits_line(
    captured_digest: dict,
    stub_engine: dict,
    recent_db: Path,
    neutralize_side_effects: None,
) -> None:
    """Sin guards critical, la línea 'Critical guards:' NO aparece en el summary."""
    stub_engine["guards"] = [{"severity": "warning", "pattern": "w"}]
    se._write_digest(ROOT, "sess-nocrit")
    assert "Critical guards" not in captured_digest["summary"]


def test_write_digest_failopen_when_recent_db_unreadable(
    captured_digest: dict,
    stub_engine: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    neutralize_side_effects: None,
) -> None:
    """Si la sub-consulta a SESSIONS_DB falla, decisions/guards quedan '' (fail-open),
    pero el digest IGUAL se guarda con el summary factual."""
    # Apuntar SESSIONS_DB a una ruta inexistente con tabla ausente → la query lanza.
    bad_db = tmp_path / "missing.db"
    bad_db.write_text("not a db")
    monkeypatch.setattr(v16_config, "SESSIONS_DB", bad_db)

    se._write_digest(ROOT, "sess-baddb")
    assert captured_digest, "save_digest debe correr aun con la sub-DB rota"
    assert captured_digest["decisions"] == ""
    assert captured_digest["guards"] == ""


def test_write_digest_narrative_appends_when_router_ok(
    captured_digest: dict,
    stub_engine: dict,
    recent_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Con narrativa habilitada y router OK, el summary lleva la frase local añadida."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ARIS4U_DIGEST_NARRATIVE", raising=False)
    monkeypatch.delenv("ARIS4U_VALIDATION_LOG", raising=False)

    class _R:
        ok = True
        text = "Construido el digest enriquecido."

    from engine.v16 import model_router

    monkeypatch.setattr(model_router, "route_local", lambda *a, **k: _R())

    se._write_digest(ROOT, "sess-narr")
    assert captured_digest["summary"].endswith("Construido el digest enriquecido.")


def test_write_digest_emits_telemetry_when_enabled(
    captured_digest: dict,
    stub_engine: dict,
    recent_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Con ARIS4U_VALIDATION_LOG + ARIS4U_LOG_FILE, se escribe el evento session_end."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ARIS4U_DIGEST_NARRATIVE", "0")
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    log_file = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log_file))

    se._write_digest(ROOT, "sess-telem")

    assert log_file.exists(), "debe escribirse el evento de telemetría"
    import json

    events = [json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    se_events = [e for e in events if e.get("event") == "session_end"]
    assert len(se_events) == 1
    ev = se_events[0]
    assert ev["session_id"] == "sess-telem"
    assert ev["decisions"] == 5
    assert ev["guards"] == 3


# ===========================================================================
# _run_analyzer_throttled
# ===========================================================================

@pytest.fixture
def throttle_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirige el archivo de estado de throttle a tmp_path y limpia env relacionado.

    Returns:
        Path del archivo de estado de throttle (no existe al inicio).
    """
    state_file = tmp_path / "last_auto_analyze"
    monkeypatch.setenv("ARIS4U_ANALYZE_STATE_FILE", str(state_file))
    monkeypatch.setenv("ARIS4U_ANALYZE_THROTTLE_SECS", "300")
    monkeypatch.delenv("ARIS4U_VALIDATION_LOG", raising=False)
    monkeypatch.delenv("ARIS4U_LOG_FILE", raising=False)
    return state_file


def test_throttled_returns_false_when_recent(
    throttle_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el último analyze fue hace <300s, devuelve False (throttled)."""
    import datetime as _dt

    now = int(_dt.datetime.now(_dt.UTC).timestamp())
    throttle_env.write_text(str(now - 10))  # hace 10s → dentro de la ventana

    assert se._run_analyzer_throttled(ROOT) is False


def test_not_throttled_returns_true_when_stale(
    throttle_env: Path, tmp_path: Path
) -> None:
    """Si pasó más del throttle, devuelve True (debe seguir).

    Sin ARIS4U_VALIDATION_LOG='1' no corre el analyzer subproceso pero igual True.
    """
    import datetime as _dt

    now = int(_dt.datetime.now(_dt.UTC).timestamp())
    throttle_env.write_text(str(now - 1000))  # hace 1000s → fuera de la ventana

    assert se._run_analyzer_throttled(ROOT) is True


def test_not_throttled_when_no_state_file(throttle_env: Path) -> None:
    """Sin archivo de estado (primera corrida), last_epoch=0 → no throttled → True."""
    assert not throttle_env.exists()
    assert se._run_analyzer_throttled(ROOT) is True


def test_throttled_emits_skip_event_once(
    throttle_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Throttled + telemetría habilitada → escribe auto_analyze_throttled y marca .skip."""
    import datetime as _dt
    import json

    now = int(_dt.datetime.now(_dt.UTC).timestamp())
    throttle_env.write_text(str(now - 10))
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    log_file = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log_file))

    assert se._run_analyzer_throttled(ROOT) is False

    assert log_file.exists()
    events = [json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    throttled = [e for e in events if e.get("event") == "auto_analyze_throttled"]
    assert len(throttled) == 1
    assert throttled[0]["throttle_secs"] == 300
    # El archivo .skip debe haberse escrito para deduplicar futuros skips.
    skip_state = Path(str(throttle_env) + ".skip")
    assert skip_state.exists()


def test_stale_runs_analyzer_and_updates_state(
    throttle_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No-throttled + VALIDATION_LOG='1' + analyzer presente: corre el subproceso,
    escribe auto_analyze_completed y ACTUALIZA el archivo de estado de throttle."""
    import datetime as _dt
    import json

    now = int(_dt.datetime.now(_dt.UTC).timestamp())
    throttle_env.write_text(str(now - 1000))
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    log_file = tmp_path / "events.jsonl"
    log_file.write_text('{"event": "x"}\n{"event": "y"}\n')  # 2 eventos
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log_file))

    # Crear un analyzer ejecutable en un root temporal con tools/.
    fake_root = tmp_path / "root"
    (fake_root / "tools").mkdir(parents=True)
    analyzer = fake_root / "tools" / "analyze_validation_log.py"
    analyzer.write_text("#!/usr/bin/env python3\nprint('ANALYSIS OK')\n")
    analyzer.chmod(0o755)

    result = se._run_analyzer_throttled(fake_root)
    assert result is True

    # El estado de throttle se actualizó (epoch ~now, ya no -1000).
    new_epoch = int(throttle_env.read_text().strip())
    assert new_epoch >= now - 5, "el estado de throttle debe avanzar al correr"

    events = [json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    completed = [e for e in events if e.get("event") == "auto_analyze_completed"]
    assert len(completed) == 1
    assert completed[0]["events_analyzed"] == 2


def test_stale_records_analyzer_failure(
    throttle_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No-throttled + analyzer que falla (exit!=0) → escribe auto_analyze_failed."""
    import datetime as _dt
    import json

    now = int(_dt.datetime.now(_dt.UTC).timestamp())
    throttle_env.write_text(str(now - 1000))
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    log_file = tmp_path / "events.jsonl"
    log_file.write_text('{"event": "x"}\n')
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log_file))

    fake_root = tmp_path / "root"
    (fake_root / "tools").mkdir(parents=True)
    analyzer = fake_root / "tools" / "analyze_validation_log.py"
    analyzer.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n")
    analyzer.chmod(0o755)

    result = se._run_analyzer_throttled(fake_root)
    assert result is True

    events = [json.loads(ln) for ln in log_file.read_text().splitlines() if ln.strip()]
    failed = [e for e in events if e.get("event") == "auto_analyze_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "analyzer exited with error"
