"""Tests de caracterización IN-PROCESS del handler UserPromptSubmit.

Caracteriza el comportamiento ACTUAL de
`hooks/dispatch/events/user_prompt_submit.py::handle` (CC=66, el peor del repo,
corre en CADA prompt) como red de seguridad ANTES de refactorizar. Los asserts
describen lo que el código hace HOY; no son aspiracionales.

El archivo de equivalencia por subproceso (`test_user_prompt_submit.py`) cubre la
paridad new-vs-.sh y el timing; este archivo cubre las RAMAS individuales con mocks,
imitando el patrón de `test_subagent_start.py`:

  - `emit_additional_context` (importado en el módulo) hace `sys.exit`; se
    monkeypatchea por un capturador → `handle()` retorna y se inspecciona el texto.
  - Los imports de `engine.v16.*` son LAZY dentro de `handle`, así que se
    monkeypatchean en sus módulos de origen ANTES de invocar `handle` (el bind
    ocurre en runtime). Idem `subprocess.run` (puente de cliente) y `_log_event`.
  - `STATE_FILE` (módulo del handler) y `ARIS4U_EVENTS_LOG` (env) se redirigen a
    tmp_path para NO tocar /tmp ni el log de producción y aislar entre tests.

Ramas cubiertas:
  - Early-exit prompt <5 chars (passthrough, sin emisión).
  - Detección de cliente desde cwd (03-clients) + export ARIS4U_CLIENT.
  - MODEL_HINT advisory por query_type (decision/research→Fable, impl/fix→Opus,
    simple→Sonnet/Haiku) bajo DEPTH_ON; ausente en modo sombra.
  - Novelty override → deep_exploration (Fable + levels 1-10 + implementation).
  - DEPTH directive (simple vs no-simple con niveles + Adaptive rationale).
  - EFFORT inyectado sólo si != medium y DEPTH_ON.
  - WAVE timer >80m.
  - TOKEN budget sobre umbral.
  - Decisiones LOCKED scoped por cliente (SELECT) + LOCKED globales.
  - AUTO-RECALL híbrido siempre presente cuando hay resultados; cap SIGALRM (fail-open).
  - Modo sombra (ARIS4U_DEPTH_PROTOCOL=0): sin DEPTH/EFFORT/MODEL_HINT, recall corre igual.
  - Persistencia de estado (query_count, GOAL, research reset) + fail-open de cada try.

Corre:
    .venv312/bin/python3 -m pytest tests/dispatch/test_user_prompt_submit_branches.py -q
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"
for _p in (str(HOOKS), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dispatch.events import user_prompt_submit as ups  # noqa: E402
from engine.v16 import v16_orchestrator as orch_mod  # noqa: E402
from engine.v16 import novelty_detector as nov_mod  # noqa: E402
from engine.v16 import session_manager as sm_mod  # noqa: E402
from engine.v16 import token_utils as tok_mod  # noqa: E402
from datetime import UTC


# ---------------------------------------------------------------------------
# Fakes de las dependencias del engine
# ---------------------------------------------------------------------------

@dataclass
class _FakeV16Result:
    """Espeja la superficie de V16QueryResult que consume handle()."""

    intent: str = "implementation"
    confidence: float = 0.9
    depth_levels: list = field(default_factory=lambda: list(range(1, 11)))
    strategy: str = "full"
    locked_decisions: list = field(default_factory=list)


class _FakeOrch:
    """Orquestador fake: process_query devuelve un resultado configurable."""

    def __init__(self, result: _FakeV16Result) -> None:
        self._result = result

    def process_query(self, query: str) -> _FakeV16Result:
        return self._result


@dataclass
class _FakeNovelty:
    is_new_domain: bool = False


class _FakeTI:
    """TokenIntelligence fake con budget_pct y effort configurables."""

    def __init__(self, budget_pct: float = 0.0, effort: str = "medium") -> None:
        self.state: dict = {"accumulated_token_estimate": 0}
        self._budget_pct = budget_pct
        self._effort = effort

    def log_query(self, query: str, query_type: str) -> None:
        return None

    def get_budget_pct(self) -> float:
        return self._budget_pct

    def get_effort_level(self, query_type: str) -> str:
        return self._effort


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Captura el texto que handle() pasa a emit_additional_context (sin sys.exit)."""
    sink: list[str] = []
    monkeypatch.setattr(ups, "emit_additional_context", lambda ctx: sink.append(ctx))
    return sink


@pytest.fixture
def no_passthrough(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Reemplaza passthrough (que hace sys.exit) por un flag + raise para cortar handle().

    handle() llama passthrough() en early-exits; en el código real eso es sys.exit.
    Lo sustituimos por una excepción dedicada que el test atrapa, registrando que
    el passthrough ocurrió.
    """
    flag: list[bool] = []

    def _pt() -> None:
        flag.append(True)
        raise _Passthrough()

    monkeypatch.setattr(ups, "passthrough", _pt)
    return flag


class _Passthrough(Exception):
    """Señal interna: handle() tomó una rama de passthrough (early-exit)."""


@pytest.fixture
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirige ups.STATE_FILE a tmp_path (no toca /tmp real)."""
    state_file = tmp_path / "session_state.json"
    monkeypatch.setattr(ups, "STATE_FILE", state_file)
    return state_file


@pytest.fixture
def isolated_events(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Apunta el event log (telemetría) a tmp_path vía ARIS4U_EVENTS_LOG."""
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_EVENTS_LOG", str(log))
    return log


@pytest.fixture
def no_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutraliza el puente de cliente (subprocess.run de write_client_bridge.sh)."""
    monkeypatch.setattr(ups.subprocess, "run", lambda *a, **k: None)


@pytest.fixture
def base_engine(monkeypatch: pytest.MonkeyPatch) -> _FakeV16Result:
    """Cablea las deps del engine a fakes deterministas y devuelve el V16 result mutable.

    Por defecto: DEPTH_PROTOCOL=1 (modo on, equivalencia con el .sh), intent=implementation,
    sin novelty, recall vacío, TI medium/0% budget. Pinear el flag aquí evita heredar el
    ARIS4U_DEPTH_PROTOCOL del ambiente (la sesión puede estar en modo sombra=0). Los tests
    de sombra lo sobreescriben a '0' explícitamente.
    """
    monkeypatch.setenv("ARIS4U_DEPTH_PROTOCOL", "1")
    result = _FakeV16Result()
    monkeypatch.setattr(orch_mod, "get_orchestrator", lambda: _FakeOrch(result))
    monkeypatch.setattr(nov_mod, "detect_novelty", lambda q: _FakeNovelty(is_new_domain=False))
    monkeypatch.setattr(tok_mod, "TokenIntelligence", lambda: _FakeTI())
    # search del recall: por defecto sin resultados.
    monkeypatch.setattr(sm_mod, "search", lambda q, limit=5, client_id=None: {})
    return result


def _emitted(sink: list[str]) -> str:
    assert len(sink) == 1, f"se esperaba 1 emisión, hubo {len(sink)}"
    return sink[0]


def _ev(prompt: str = "implementa el modulo de pagos completo", cwd: str | None = None) -> dict:
    """Construye el payload del evento; cwd neutro por defecto (no es 03-clients)."""
    return {"prompt": prompt, "cwd": cwd or str(ROOT)}


# ---------------------------------------------------------------------------
# Early-exit
# ---------------------------------------------------------------------------

def test_short_prompt_passthrough(
    captured: list[str], no_passthrough: list[bool], isolated_state: Path
) -> None:
    """Prompt <5 chars → passthrough (sin emit)."""
    with pytest.raises(_Passthrough):
        ups.handle("UserPromptSubmit", {"prompt": "hi"})
    assert no_passthrough == [True]
    assert captured == []


def test_novelty_failopen(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detect_novelty lanzando → fail-open: cae al path normal (sin override) sin crashear."""
    monkeypatch.setattr(
        nov_mod, "detect_novelty", lambda q: (_ for _ in ()).throw(RuntimeError("x"))
    )
    base_engine.intent = "implementation"
    base_engine.depth_levels = [1, 2, 3]
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    # Sin novelty override: usa los niveles del v16_result, no los 10 de deep_exploration.
    assert "DEPTH: implementation | RECALL, RESEARCH, ANALYZE" in out
    assert "nuevo dominio" not in out


# ---------------------------------------------------------------------------
# Detección de cliente
# ---------------------------------------------------------------------------

def test_client_detected_and_exported(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cwd under 03-clients/client-b-platform → ARIS4U_CLIENT=client-b (suffix stripped)."""
    monkeypatch.delenv("ARIS4U_CLIENT", raising=False)
    cwd = "/Users/x/projects/03-clients/client-b-platform/src"
    # capturar el client_id pasado a search.
    seen: dict = {}
    monkeypatch.setattr(
        sm_mod, "search", lambda q, limit=5, client_id=None: seen.update(cid=client_id) or {}
    )
    ups.handle("UserPromptSubmit", _ev(cwd=cwd))
    import os

    assert os.environ.get("ARIS4U_CLIENT") == "client-b"
    assert seen.get("cid") == "client-b"


def test_no_client_outside_03clients(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cwd fuera de 03-clients → no se setea ARIS4U_CLIENT (queda lo previo / vacío)."""
    monkeypatch.delenv("ARIS4U_CLIENT", raising=False)
    ups.handle("UserPromptSubmit", _ev(cwd="/tmp/somewhere"))
    import os

    assert os.environ.get("ARIS4U_CLIENT", "") == ""


# ---------------------------------------------------------------------------
# MODEL_HINT advisory
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "intent,dom_model",
    [
        # V18 Fase A: el hint orienta SUBAGENTES; 'model' = modelo dominante del fan-out.
        ("decision", "opus"),
        ("research", "sonnet"),
        ("implementation", "sonnet"),
        ("fix", "sonnet"),
        ("simple", "haiku"),
    ],
)
def test_model_hint_by_intent(
    intent: str,
    dom_model: str,
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROUTING (V18): siempre emite la línea de routing con las 3 tiers; telemetría lleva
    el modelo DOMINANTE del fan-out por intención. No depende de DEPTH_PROTOCOL."""
    base_engine.intent = intent
    base_engine.depth_levels = [1] if intent == "simple" else [1, 2, 3]
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "🧭 ROUTING" in out
    assert "model=" in out and "opus" in out and "sonnet" in out and "haiku" in out
    events = [json.loads(ln) for ln in isolated_events.read_text().splitlines()]
    mh = [e for e in events if e.get("event") == "model_hint"]
    assert mh and mh[-1]["model"] == dom_model and mh[-1]["intent"] == intent


def test_model_hint_logged_to_events(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La telemetría model_hint se escribe al event log con el modelo dominante."""
    base_engine.intent = "decision"
    base_engine.depth_levels = [1, 2, 3]
    ups.handle("UserPromptSubmit", _ev())
    lines = isolated_events.read_text().splitlines()
    events = [json.loads(ln) for ln in lines]
    mh = [e for e in events if e.get("event") == "model_hint"]
    assert mh and mh[-1]["model"] == "opus" and mh[-1]["intent"] == "decision"


# ---------------------------------------------------------------------------
# Novelty override
# ---------------------------------------------------------------------------

def test_novelty_override_deep_exploration(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """is_new_domain → ROUTING con dominante opus (exploración profunda) + DEPTH 10 niveles."""
    monkeypatch.setenv("ARIS4U_DEPTH_PROTOCOL", "1")
    base_engine.intent = "simple"  # se sobreescribe a implementation por novelty
    monkeypatch.setattr(nov_mod, "detect_novelty", lambda q: _FakeNovelty(is_new_domain=True))
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "🧭 ROUTING" in out and "≈opus" in out  # novelty deep → dominante opus
    # query_type pasa a implementation → DEPTH no-simple con todos los niveles.
    assert "DEPTH: implementation" in out
    assert "CAPTURE" in out  # nivel 10 presente


# ---------------------------------------------------------------------------
# DEPTH directive
# ---------------------------------------------------------------------------

def test_depth_simple(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """intent=simple → 'DEPTH: simple' y NO bloque RECALL (gate query_type=='simple')."""
    monkeypatch.setenv("ARIS4U_DEPTH_PROTOCOL", "1")
    base_engine.intent = "simple"
    base_engine.depth_levels = [1]
    ups.handle("UserPromptSubmit", _ev(prompt="que hora es ahora"))
    out = _emitted(captured)
    assert "DEPTH: simple" in out
    assert "🧠 RECALL" not in out


def test_depth_nonsimple_with_levels_and_adaptive(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-simple → 'DEPTH: <intent> | <niveles>' + línea Adaptive con la estrategia F2."""
    monkeypatch.setenv("ARIS4U_DEPTH_PROTOCOL", "1")
    base_engine.intent = "implementation"
    base_engine.depth_levels = [1, 2, 3]
    base_engine.strategy = "full"
    base_engine.confidence = 0.8
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "DEPTH: implementation | RECALL, RESEARCH, ANALYZE" in out
    assert "[Adaptive: V16_F2_full]" in out


# ---------------------------------------------------------------------------
# EFFORT
# ---------------------------------------------------------------------------

def test_effort_injected_when_not_medium(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_effort_level != 'medium' y DEPTH_ON → 'EFFORT: HIGH'."""
    monkeypatch.setenv("ARIS4U_DEPTH_PROTOCOL", "1")
    monkeypatch.setattr(tok_mod, "TokenIntelligence", lambda: _FakeTI(effort="high"))
    ups.handle("UserPromptSubmit", _ev())
    assert "EFFORT: HIGH" in _emitted(captured)


def test_effort_omitted_when_medium(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """effort == 'medium' → no se inyecta EFFORT."""
    monkeypatch.setenv("ARIS4U_DEPTH_PROTOCOL", "1")
    monkeypatch.setattr(tok_mod, "TokenIntelligence", lambda: _FakeTI(effort="medium"))
    ups.handle("UserPromptSubmit", _ev())
    assert "EFFORT:" not in _emitted(captured)


# ---------------------------------------------------------------------------
# WAVE timer + TOKEN budget
# ---------------------------------------------------------------------------

def test_wave_timer_warns_over_80m(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wave_start_time hace >80m → línea ⏱️ WAVE."""
    from datetime import datetime, timedelta, timezone

    old = (datetime.now(UTC) - timedelta(minutes=120)).isoformat()
    isolated_state.write_text(json.dumps({"wave_start_time": old}))
    ups.handle("UserPromptSubmit", _ev())
    assert "⏱️ WAVE:" in _emitted(captured)


def test_token_budget_warns_over_threshold(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """budget_pct >= umbral → línea TOKEN."""
    # TOKEN_WARN_THRESHOLD_PCT default = 70 (porcentaje entero, no fracción).
    monkeypatch.setattr(tok_mod, "TokenIntelligence", lambda: _FakeTI(budget_pct=99.0))
    ups.handle("UserPromptSubmit", _ev())
    assert "TOKEN:" in _emitted(captured)


# ---------------------------------------------------------------------------
# LOCKED decisions
# ---------------------------------------------------------------------------

def test_locked_decisions_scoped_by_client(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con ARIS4U_CLIENT, el SELECT scoped emite '[CLIENTE: x] Decisiones previas:'."""
    monkeypatch.setenv("ARIS4U_CLIENT", "client-b")

    class _FakeCur:
        def fetchall(self):
            return [("Usar Flyway siempre", "infra", "0601a")]

    class _FakeDB:
        def execute(self, sql, params):
            return _FakeCur()

        def close(self):
            return None

    monkeypatch.setattr(sm_mod, "_connect", lambda: _FakeDB())
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "[CLIENTE: client-b] Decisiones previas:" in out
    assert "[0601a] (infra): Usar Flyway siempre" in out


def test_locked_global_decisions(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v16_result.locked_decisions → líneas 'LOCKED [ref]: ...' (cap 2)."""
    monkeypatch.delenv("ARIS4U_CLIENT", raising=False)
    base_engine.locked_decisions = [
        {"session_ref": "g1", "decision": "Nunca tocar finanzas"},
        {"session_ref": "g2", "decision": "RLS obligatorio"},
        {"session_ref": "g3", "decision": "no aparece (cap 2)"},
    ]
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "LOCKED [g1]: Nunca tocar finanzas" in out
    assert "LOCKED [g2]: RLS obligatorio" in out
    assert "g3" not in out


# ---------------------------------------------------------------------------
# AUTO-RECALL híbrido
# ---------------------------------------------------------------------------

def test_recall_present_with_results(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search devolviendo semantic/decisions/guards → bloque 🧠 RECALL con sus líneas."""
    fake = {
        "semantic": [{"similarity": 0.71, "source": "obs", "source_id": "9", "text": "memoria previa"}],
        "decisions": [{"domain": "infra", "decision": "decidimos X"}],
        "guards": [{"pattern": "no latest", "prevention": "pin version"}],
    }
    monkeypatch.setattr(sm_mod, "search", lambda q, limit=5, client_id=None: fake)
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "🧠 RECALL (memoria ARIS4U relevante):" in out
    assert "~0.71 [obs#9] memoria previa" in out
    assert "· (infra) decidimos X" in out
    assert "! no latest -> pin version" in out


def test_recall_atom_channel_boost(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boost #4: un átomo (fact + problem_class O structural_signature) sale en su canal
    dedicado 🧬 ÁTOMOS con la línea reflexiva PISO-no-techo, y se cuenta en n_atoms."""
    fake = {
        "semantic": [
            # átomo por problem_class
            {"similarity": 0.62, "source": "decisions", "source_id": "10",
             "text": "modela el surge como proceso estocástico", "mem_type": "fact",
             "problem_class": "stochastic-process", "validity_domain": "demanda con picos"},
            # atom by structural_signature (no problem_class — operational lab case)
            {"similarity": 0.55, "source": "decisions", "source_id": "11",
             "text": "idempotency guard antes de escribir", "mem_type": "fact",
             "structural_signature": "concurrency-control|guard|write-path"},
            # memoria general (no átomo)
            {"similarity": 0.71, "source": "obs", "source_id": "9", "text": "memoria previa"},
        ],
        "decisions": [], "guards": [],
    }
    monkeypatch.setattr(sm_mod, "search", lambda q, limit=5, client_id=None: fake)
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "🧬 ÁTOMOS aplicables" in out
    assert "átomo[stochastic-process]" in out
    assert "átomo[concurrency-control]" in out       # label derivado de la firma
    assert "PISO no techo" in out
    assert "~0.71 [obs#9] memoria previa" in out      # la general sigue saliendo
    events = [json.loads(ln) for ln in isolated_events.read_text().splitlines()]
    ar = [e for e in events if e.get("event") == "auto_recall"]
    assert ar and ar[0].get("n_atoms") == 2


def test_skeleton_injected_on_build_intent(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build flow: en intención implementation, el átomo #1 muy relevante inyecta su PLANTILLA
    (skeleton). Telemetría n_skeletons=1. En intención simple NO se inyecta."""
    base_engine.intent = "implementation"
    fake = {
        "semantic": [
            {"similarity": 0.62, "source": "decisions", "source_id": "100",
             "text": "ledger append-only", "mem_type": "fact",
             "structural_signature": "ledger-append-only|deterministic"},
        ],
        "decisions": [], "guards": [],
    }
    monkeypatch.setattr(sm_mod, "search", lambda q, limit=5, client_id=None: fake)
    monkeypatch.setattr(sm_mod, "get_skeleton",
                        lambda sid: "create table public.<movements> (...)\n-- trigger ...")
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "📐 PLANTILLA" in out
    assert "create table public.<movements>" in out
    events = [json.loads(ln) for ln in isolated_events.read_text().splitlines()]
    ar = [e for e in events if e.get("event") == "auto_recall"]
    assert ar and ar[0].get("n_skeletons") == 1


def test_pick_skeleton_atom_prefers_software_pattern() -> None:
    """El skeleton inyectado prefiere el patrón de SOFTWARE (artifact_type en la firma) sobre el
    átomo puro-matemático, aunque el matemático tenga mayor similitud (sesgo medido idempotencia)."""
    atoms = [
        {"structural_signature": "probability-estimation||stochastic"},  # math, sin artifact_type
        {"structural_signature": "-|idempotency-guard|deterministic"},   # software pattern
    ]
    assert ups._pick_skeleton_atom(atoms) == 1
    # si el #1 ya es patrón de software, se queda con el #1
    assert ups._pick_skeleton_atom([{"structural_signature": "-|access-control|deterministic"}]) == 0
    # si ninguno tiene artifact_type, default al #1
    assert ups._pick_skeleton_atom([{"structural_signature": "queueing||stochastic"}]) == 0


def test_skeleton_not_injected_on_simple_intent(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """En intención simple (o no-build) la plantilla NO se inyecta aunque el átomo tenga skeleton."""
    base_engine.intent = "simple"
    base_engine.depth_levels = [1]
    fake = {
        "semantic": [
            {"similarity": 0.62, "source": "decisions", "source_id": "100",
             "text": "ledger append-only", "mem_type": "fact",
             "structural_signature": "ledger-append-only|deterministic"},
        ],
        "decisions": [], "guards": [],
    }
    monkeypatch.setattr(sm_mod, "search", lambda q, limit=5, client_id=None: fake)
    monkeypatch.setattr(sm_mod, "get_skeleton", lambda sid: "create table ...")
    ups.handle("UserPromptSubmit", _ev())
    assert "📐 PLANTILLA" not in _emitted(captured)


def test_recall_atom_below_floor_demoted_to_general(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un átomo bajo el piso ATOM_RECALL_MIN_SIM no abre el canal 🧬 (anti-ruido); si igual
    es el único hit, cae al canal general sin la cabecera de átomos."""
    fake = {
        "semantic": [
            {"similarity": 0.30, "source": "decisions", "source_id": "12",
             "text": "patrón débilmente relacionado", "mem_type": "fact",
             "problem_class": "mathematical-optimization"},
        ],
        "decisions": [], "guards": [],
    }
    monkeypatch.setattr(sm_mod, "search", lambda q, limit=5, client_id=None: fake)
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "🧬 ÁTOMOS aplicables" not in out
    events = [json.loads(ln) for ln in isolated_events.read_text().splitlines()]
    ar = [e for e in events if e.get("event") == "auto_recall"]
    assert ar and ar[0].get("n_atoms") == 0


def test_recall_failopen_on_search_error(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search lanzando → fail-open: handle() emite igual, sin 🧠 RECALL, sin crashear."""
    def _boom(q, limit=5, client_id=None):
        raise RuntimeError("ollama caido")

    monkeypatch.setattr(sm_mod, "search", _boom)
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "🧠 RECALL" not in out
    assert "DEPTH:" in out  # el resto del contexto sigue presente


def test_recall_logs_auto_recall_event(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Se emite telemetría auto_recall con recall_id y latency_ms."""
    monkeypatch.setattr(sm_mod, "search", lambda q, limit=5, client_id=None: {"semantic": []})
    ups.handle("UserPromptSubmit", _ev())
    events = [json.loads(ln) for ln in isolated_events.read_text().splitlines()]
    ar = [e for e in events if e.get("event") == "auto_recall"]
    assert ar and "recall_id" in ar[0] and "latency_ms" in ar[0]


# ---------------------------------------------------------------------------
# Modo sombra (ARIS4U_DEPTH_PROTOCOL=0)
# ---------------------------------------------------------------------------

def test_shadow_mode_omits_cognition_keeps_recall(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DEPTH_PROTOCOL=0: sin DEPTH/EFFORT; el RECALL sí corre (foso vivo). V18: el ROUTING
    ahora SÍ aparece aunque DEPTH=0 (auto-inyección desacoplada del depth protocol)."""
    monkeypatch.setenv("ARIS4U_DEPTH_PROTOCOL", "0")
    monkeypatch.setattr(tok_mod, "TokenIntelligence", lambda: _FakeTI(effort="high"))
    fake = {"semantic": [{"similarity": 0.5, "source": "o", "source_id": "1", "text": "m"}]}
    monkeypatch.setattr(sm_mod, "search", lambda q, limit=5, client_id=None: fake)
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "DEPTH:" not in out
    assert "EFFORT:" not in out
    assert "🧭 ROUTING" in out  # V18: routing visible aun en modo sombra
    assert "🧠 RECALL" in out  # el foso sigue


def test_shadow_mode_recall_runs_even_for_simple(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """En sombra el gate (not DEPTH_ON) hace que el recall corra aun con intent=simple."""
    monkeypatch.setenv("ARIS4U_DEPTH_PROTOCOL", "0")
    base_engine.intent = "simple"
    base_engine.depth_levels = [1]
    fake = {"semantic": [{"similarity": 0.4, "source": "o", "source_id": "2", "text": "x"}]}
    monkeypatch.setattr(sm_mod, "search", lambda q, limit=5, client_id=None: fake)
    ups.handle("UserPromptSubmit", _ev(prompt="pregunta simple corta"))
    assert "🧠 RECALL" in _emitted(captured)


# ---------------------------------------------------------------------------
# Persistencia de estado
# ---------------------------------------------------------------------------

def test_state_query_count_and_goal(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """handle() incrementa query_count, fija session_goal (no-simple) y persiste el estado."""
    ups.handle("UserPromptSubmit", _ev())
    state = json.loads(isolated_state.read_text())
    assert state["query_count"] == 1
    assert state["last_query_type"] == "implementation"
    assert "session_goal" in state
    # intent implementation resetea research_done_for_current.
    assert state["research_done_for_current"] is False


def test_state_failopen_invalid_json(
    captured: list[str],
    isolated_state: Path,
    isolated_events: Path,
    no_bridge: None,
    base_engine: _FakeV16Result,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STATE_FILE con JSON inválido no rompe handle(); reinicia el estado limpio."""
    isolated_state.write_text("{ no json :::")
    ups.handle("UserPromptSubmit", _ev())
    out = _emitted(captured)
    assert "DEPTH:" in out
    state = json.loads(isolated_state.read_text())
    assert state["query_count"] == 1
