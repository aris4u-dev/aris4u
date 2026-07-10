"""Tests del router de modelos Claude para subagentes (V18 Fase A)."""
from __future__ import annotations

import pytest

from tools import model_router as mr


# ── route_model: subtarea domina ─────────────────────────────────────────────────
@pytest.mark.parametrize(
    "subtask,expected",
    [
        ("synthesis", "opus"),
        ("synthesize", "opus"),
        ("judge", "opus"),
        ("architecture", "opus"),
        ("audit", "opus"),
        ("verify", "sonnet"),
        ("review", "sonnet"),
        ("search", "sonnet"),
        ("explore", "sonnet"),
        ("summarize", "sonnet"),
        ("implement", "sonnet"),
        ("classify", "haiku"),
        ("format", "haiku"),
        ("count", "haiku"),
        ("label", "haiku"),
    ],
)
def test_route_model_by_subtask(subtask: str, expected: str) -> None:
    assert mr.route_model(subtask) == expected


def test_route_model_normalizes_case_and_separators() -> None:
    assert mr.route_model("SYNTHESIS") == "opus"
    assert mr.route_model("extract-structured") == "sonnet"
    assert mr.route_model("extract field") == "haiku"


# ── route_model: fallback por intención ──────────────────────────────────────────
@pytest.mark.parametrize(
    "intent,expected",
    [
        ("decision", "opus"),
        ("research", "sonnet"),
        ("implementation", "sonnet"),
        ("fix", "sonnet"),
        ("simple", "haiku"),
    ],
)
def test_route_model_by_intent(intent: str, expected: str) -> None:
    assert mr.route_model(intent=intent) == expected


def test_route_model_subtask_beats_intent() -> None:
    # subtask 'format' (haiku) domina sobre intent 'decision' (opus)
    assert mr.route_model("format", intent="decision") == "haiku"


def test_route_model_default_is_sonnet() -> None:
    # sin señal reconocible → el grueso (nunca opus asumido, nunca heredar Fable)
    assert mr.route_model() == "sonnet"
    assert mr.route_model("gobbledygook") == "sonnet"
    assert mr.route_model(intent="unknown_intent") == "sonnet"


def test_route_model_never_returns_fable() -> None:
    for st in ["synthesis", "verify", "format", None, "weird"]:
        assert mr.route_model(st) != "fable"


# ── session_model ────────────────────────────────────────────────────────────────
def test_session_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_CLAUDE_MODEL", "claude-fable-5[1m]")
    assert mr.session_model() == "fable"
    monkeypatch.setenv("ARIS4U_CLAUDE_MODEL", "claude-opus-4-8")
    assert mr.session_model() == "opus"


def test_session_model_unknown_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_CLAUDE_MODEL", "some-other-model")
    assert mr.session_model() == ""


# ── routing_hint ─────────────────────────────────────────────────────────────────
def test_routing_hint_mentions_model_and_tiers() -> None:
    h = mr.routing_hint("implementation")
    assert "model=" in h
    assert "opus" in h and "sonnet" in h and "haiku" in h


def test_routing_hint_warns_when_session_is_expensive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_CLAUDE_MODEL", "claude-fable-5[1m]")
    h = mr.routing_hint("fix")
    assert "fable" in h.lower() and "CARO" in h


def test_routing_hint_no_warn_when_session_cheap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_CLAUDE_MODEL", "claude-sonnet-4-6")
    h = mr.routing_hint("fix")
    assert "CARO" not in h


def test_routing_hint_novelty_deep_is_opus_dominant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIS4U_CLAUDE_MODEL", raising=False)
    h = mr.routing_hint(None, novelty_deep=True)
    assert "≈opus" in h
