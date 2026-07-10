"""Tests del recall adaptado al modelo (V18 Fase D) — model_router.recall_tier/tier_caps."""
from __future__ import annotations

import pytest

from tools import model_router as mr


@pytest.mark.parametrize(
    "model,tier",
    [
        ("claude-opus-4-8", "full"),
        ("claude-fable-5[1m]", "full"),
        ("claude-sonnet-4-6", "compact"),
        ("claude-haiku-4-5", "guard_only"),
        ("modelo-desconocido", "full"),  # sin certeza → no recortar
    ],
)
def test_recall_tier_by_model(model: str, tier: str) -> None:
    assert mr.recall_tier(model) == tier


def test_recall_tier_uses_session_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_CLAUDE_MODEL", "claude-sonnet-4-6")
    assert mr.recall_tier() == "compact"
    monkeypatch.setenv("ARIS4U_CLAUDE_MODEL", "claude-haiku-4-5")
    assert mr.recall_tier() == "guard_only"


def test_tier_caps_shape() -> None:
    full = mr.tier_caps("full")
    compact = mr.tier_caps("compact")
    guard = mr.tier_caps("guard_only")
    # full no recorta (valores altos), guard_only elimina semantic+decisions.
    assert full["semantic"] >= 999 and full["decisions"] >= 999
    assert compact["decisions"] < full["decisions"]
    assert guard["semantic"] == 0 and guard["decisions"] == 0 and guard["guards"] > 0


def test_tier_caps_invalid_falls_back_to_full() -> None:
    assert mr.tier_caps("nonsense") == mr.tier_caps("full")


def test_tier_caps_returns_copy() -> None:
    # No debe mutar el mapa interno.
    c = mr.tier_caps("compact")
    c["semantic"] = -1
    assert mr.tier_caps("compact")["semantic"] == 3
