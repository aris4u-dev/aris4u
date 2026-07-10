"""Characterization unit tests for V16Orchestrator.process_query.

These tests pin the EXACT behavior of process_query (intent/confidence/depth/
effort/hooks/strategy/locked_decisions/guards/error wiring) by mocking the
lazily-imported F1/F2/F3 dependencies. No Ollama/MLX is loaded; everything is
deterministic and fast.

The orchestrator imports its dependencies with module-local `from .x import y`
statements evaluated at call time, so patching the *source* module attribute
(e.g. engine.v16.f1_classifier.classify_v16_with_confidence) intercepts them.

Purpose: provide a behavior-preserving safety net for the CC refactor of
process_query (CC 23 -> <=10).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from engine.v16.v16_orchestrator import (
    V16Orchestrator,
    V16QueryResult,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
@dataclass
class _FakeDecision:
    """Mimics f2_cognicion.CognicionResult (only fields process_query reads)."""

    depth_levels: list[int]
    effort_level: str
    hooks_active: list[str]
    strategy: str


class _FakeF2:
    """Stand-in for the F2 cognicion engine."""

    def __init__(self, decision: _FakeDecision) -> None:
        self._decision = decision
        self.calls: list[dict] = []

    def decide(self, **kwargs: Any) -> _FakeDecision:
        self.calls.append(kwargs)
        return self._decision


class _FakeMemoria:
    """Stand-in for the F3 MemoriaEngine."""

    def __init__(self, state: dict | None = None) -> None:
        self._state = state if state is not None else {}
        self.events: list[tuple[str, dict]] = []

    def load_state(self, key: str, default: Any = None) -> Any:
        # _load_budget calls load_state("token_intelligence"); return the fake
        # state dict regardless of key so tests can inject budget values.
        return self._state if self._state else default

    def append_event(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


def _patch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    intent: str = "decision",
    confidence: float = 0.91,
    decision: _FakeDecision | None = None,
    memoria: _FakeMemoria | None = None,
    locked: list[dict] | None = None,
    guards: list[dict] | None = None,
) -> dict[str, Any]:
    """Patch all happy-path dependencies of process_query.

    Returns the test doubles so callers can assert on interactions.
    """
    decision = decision or _FakeDecision(
        depth_levels=[1, 2, 3, 4],
        effort_level="high",
        hooks_active=["recall", "verify"],
        strategy="deep",
    )
    f2 = _FakeF2(decision)
    memoria = memoria or _FakeMemoria(state={})
    locked = locked if locked is not None else [{"id": 1, "decision": "x"}]
    raw_guards = guards if guards is not None else [
        {"severity": "critical", "rule": "g1"},
        {"severity": "low", "rule": "g2"},
    ]

    import engine.v16.f1_classifier as f1_mod
    import engine.v16.f2_cognicion as f2_mod
    import engine.v16.session_manager as sm_mod
    import engine.v16.v16_orchestrator as orch_mod

    monkeypatch.setattr(
        f1_mod, "classify_v16_with_confidence",
        lambda q: (intent, confidence),
    )
    monkeypatch.setattr(f2_mod, "create_cognicion_engine", lambda *a, **k: f2)
    monkeypatch.setattr(orch_mod, "_get_or_create_memoria", lambda: memoria)
    monkeypatch.setattr(sm_mod, "get_locked_decisions", lambda q, limit=3: locked)
    monkeypatch.setattr(sm_mod, "get_all_guards", lambda: raw_guards)
    # Disable F5/F6 factory side effects (they only log; keep them inert).
    monkeypatch.setattr(orch_mod, "_create_f5_validator", lambda: object())
    monkeypatch.setattr(orch_mod, "_create_f6_comunicacion", lambda: object())
    # Reset the F2/F5/F6 module-level singletons so _get_or_create rebuilds.
    monkeypatch.setattr(orch_mod, "_f2_engine", None)
    monkeypatch.setattr(orch_mod, "_f5_validator", None)
    monkeypatch.setattr(orch_mod, "_f6_comunicacion", None)

    return {"f2": f2, "memoria": memoria, "locked": locked, "guards": raw_guards}


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_happy_path_full_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every field is populated from its respective phase."""
    doubles = _patch_pipeline(monkeypatch)
    orch = V16Orchestrator()

    result = orch.process_query("decide between A and B")

    assert isinstance(result, V16QueryResult)
    # F1
    assert result.intent == "decision"
    assert result.confidence == 0.91
    # F2
    assert result.depth_levels == [1, 2, 3, 4]
    assert result.effort_level == "high"
    assert result.hooks_active == ["recall", "verify"]
    assert result.strategy == "deep"
    # F3
    assert result.locked_decisions == doubles["locked"]
    assert result.guards == [{"severity": "critical", "rule": "g1"}]  # only critical
    # no error
    assert result.error is None
    assert result.error_module is None


def test_query_truncated_to_500_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2 complexity is derived from the 500-char-truncated query word count."""
    _patch_pipeline(monkeypatch)
    f2 = _FakeF2(_FakeDecision([1], "low", [], "default"))
    import engine.v16.f2_cognicion as f2_mod
    monkeypatch.setattr(f2_mod, "create_cognicion_engine", lambda *a, **k: f2)
    import engine.v16.v16_orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_f2_engine", None)

    orch = V16Orchestrator()
    # 600 single-char "words"; truncated to 500 chars -> "aaaa..." is ONE word
    # after truncation because there are no spaces. Use spaced words instead:
    long_query = " ".join(["w"] * 600)  # 1199 chars, 600 words pre-truncation
    orch.process_query(long_query)

    truncated_words = len(long_query[:500].split())
    assert f2.calls[0]["complexity"] == truncated_words * 2


def test_f3_event_logged_and_session_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """F3 receives an event_logged append and session_log accumulates."""
    doubles = _patch_pipeline(monkeypatch)
    orch = V16Orchestrator()

    orch.process_query("hello")

    memoria = doubles["memoria"]
    assert len(memoria.events) == 1
    kind, payload = memoria.events[0]
    assert kind == "event_logged"
    assert payload["intent"] == "decision"
    assert len(orch.session_log) == 1
    assert orch.session_log[0]["query"] == "hello"


def test_budget_loaded_from_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2.decide receives budget pulled from F3 state when present."""
    mem = _FakeMemoria(state={"budget_remaining": 42000, "budget_max": 150000})
    doubles = _patch_pipeline(monkeypatch, memoria=mem)
    orch = V16Orchestrator()

    orch.process_query("q")

    call = doubles["f2"].calls[0]
    assert call["budget_remaining"] == 42000
    assert call["budget_max"] == 150000


def test_budget_defaults_when_no_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2.decide gets safe defaults when state load returns empty."""
    mem = _FakeMemoria(state={})
    doubles = _patch_pipeline(monkeypatch, memoria=mem)
    orch = V16Orchestrator()

    orch.process_query("q")

    call = doubles["f2"].calls[0]
    assert call["budget_remaining"] == 100000
    assert call["budget_max"] == 200000


# --------------------------------------------------------------------------- #
# F1 failure handling
# --------------------------------------------------------------------------- #
def test_f1_single_failure_keeps_simple_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """One F1 failure -> intent stays 'simple', pipeline continues (no error set)."""
    _patch_pipeline(monkeypatch)
    import engine.v16.f1_classifier as f1_mod

    def boom(_q: str):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(f1_mod, "classify_v16_with_confidence", boom)
    orch = V16Orchestrator()

    result = orch.process_query("q")

    assert result.intent == "simple"
    assert result.confidence == 0.0
    assert result.error is None  # not yet at threshold
    # F2 still ran (defaults overwritten by fake decision)
    assert result.effort_level == "high"


def test_f1_three_failures_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Third consecutive F1 failure sets error/error_module='F1' and returns early."""
    _patch_pipeline(monkeypatch)
    import engine.v16.f1_classifier as f1_mod

    def boom(_q: str):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(f1_mod, "classify_v16_with_confidence", boom)
    orch = V16Orchestrator()

    orch.process_query("q1")
    orch.process_query("q2")
    result = orch.process_query("q3")

    assert result.error == "ollama down"
    assert result.error_module == "F1"
    # Early return -> F2 defaults retained (never overwritten)
    assert result.effort_level == "medium"
    assert result.strategy == "default"
    assert result.depth_levels == [1]


def test_f1_success_resets_failure_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """A success between failures resets the consecutive-failure counter."""
    _patch_pipeline(monkeypatch)
    import engine.v16.f1_classifier as f1_mod

    calls = {"n": 0}

    def flaky(_q: str):
        calls["n"] += 1
        if calls["n"] in (1, 2):
            raise RuntimeError("transient")
        return ("fix", 0.7)

    monkeypatch.setattr(f1_mod, "classify_v16_with_confidence", flaky)
    orch = V16Orchestrator()

    orch.process_query("a")  # fail 1
    orch.process_query("b")  # fail 2
    r3 = orch.process_query("c")  # success -> reset

    assert orch._f1_failure_count == 0
    assert r3.intent == "fix"
    assert r3.error is None


# --------------------------------------------------------------------------- #
# F2 failure handling
# --------------------------------------------------------------------------- #
def test_f2_failure_uses_defaults_no_early_return(monkeypatch: pytest.MonkeyPatch) -> None:
    """F2 failure keeps default depth/effort/strategy but F3 still runs."""
    doubles = _patch_pipeline(monkeypatch)
    import engine.v16.f2_cognicion as f2_mod

    def boom(*_a, **_k):
        raise RuntimeError("f2 broke")

    monkeypatch.setattr(f2_mod, "create_cognicion_engine", boom)
    import engine.v16.v16_orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_f2_engine", None)
    orch = V16Orchestrator()

    result = orch.process_query("q")

    assert result.effort_level == "medium"
    assert result.strategy == "default"
    assert result.depth_levels == [1]
    # F1 still applied, F3 still ran (guards filtered)
    assert result.intent == "decision"
    assert result.guards == [{"severity": "critical", "rule": "g1"}]
    assert result.error is None  # below threshold
    assert len(doubles["memoria"].events) == 1


def test_f2_three_failures_sets_error_but_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    """Third F2 failure sets error='F2' yet F3 phase still executes (no early return)."""
    doubles = _patch_pipeline(monkeypatch)
    import engine.v16.f2_cognicion as f2_mod

    def boom(*_a, **_k):
        raise RuntimeError("f2 broke")

    monkeypatch.setattr(f2_mod, "create_cognicion_engine", boom)
    import engine.v16.v16_orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "_f2_engine", None)
    orch = V16Orchestrator()

    orch.process_query("q1")
    orch.process_query("q2")
    result = orch.process_query("q3")

    assert result.error_module == "F2"
    # _get_or_create swallows the underlying error and returns None, so
    # process_query raises its own message, which is what surfaces here.
    assert result.error == "F2 engine initialization failed"
    # F3 ran despite F2 error
    assert result.locked_decisions == doubles["locked"]


# --------------------------------------------------------------------------- #
# F3 recall fallbacks
# --------------------------------------------------------------------------- #
def test_f3_locked_fallback_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """When get_locked_decisions fails, process_query falls back to search()."""
    _patch_pipeline(monkeypatch)
    import engine.v16.session_manager as sm_mod

    def boom(*_a, **_k):
        raise RuntimeError("no locked")

    monkeypatch.setattr(sm_mod, "get_locked_decisions", boom)
    monkeypatch.setattr(
        sm_mod, "search",
        lambda q, limit=3: {"decisions": [{"id": 99}]},
    )
    orch = V16Orchestrator()

    result = orch.process_query("q")

    assert result.locked_decisions == [{"id": 99}]


def test_f3_guards_failure_leaves_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_all_guards failure leaves guards as the default empty list."""
    _patch_pipeline(monkeypatch)
    import engine.v16.session_manager as sm_mod

    def boom():
        raise RuntimeError("no guards")

    monkeypatch.setattr(sm_mod, "get_all_guards", boom)
    orch = V16Orchestrator()

    result = orch.process_query("q")

    assert result.guards == []


def test_guards_capped_at_four(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the first 4 critical guards are kept."""
    many = [{"severity": "critical", "rule": f"g{i}"} for i in range(10)]
    _patch_pipeline(monkeypatch, guards=many)
    orch = V16Orchestrator()

    result = orch.process_query("q")

    assert len(result.guards) == 4
    assert result.guards == many[:4]


# --------------------------------------------------------------------------- #
# Validation / formatting toggles (currently deferred, must not raise)
# --------------------------------------------------------------------------- #
def test_validation_and_formatting_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """apply_validation/apply_formatting=False skip F5/F6 factory calls."""
    _patch_pipeline(monkeypatch)
    import engine.v16.v16_orchestrator as orch_mod

    f5_calls = {"n": 0}
    f6_calls = {"n": 0}
    monkeypatch.setattr(
        orch_mod, "_create_f5_validator",
        lambda: f5_calls.__setitem__("n", f5_calls["n"] + 1),
    )
    monkeypatch.setattr(
        orch_mod, "_create_f6_comunicacion",
        lambda: f6_calls.__setitem__("n", f6_calls["n"] + 1),
    )
    orch = V16Orchestrator()

    result = orch.process_query("q", apply_validation=False, apply_formatting=False)

    assert f5_calls["n"] == 0
    assert f6_calls["n"] == 0
    assert result.intent == "decision"


def test_to_dict_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Result serializes with all expected keys."""
    _patch_pipeline(monkeypatch)
    orch = V16Orchestrator()

    d = orch.process_query("q").to_dict()

    assert set(d) == {
        "intent", "confidence", "depth_levels", "effort_level",
        "hooks_active", "strategy", "locked_decisions", "guards",
        "validation_result", "format_directive", "error", "error_module",
    }
    assert d["intent"] == "decision"
