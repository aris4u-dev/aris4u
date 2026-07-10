"""Tests del reporte de routing/costo de subagentes (V18 Fase C)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools import cost_report as cr


def _write_log(tmp_path: Path, events: list[dict]) -> Path:
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return p


def _dispatch(model=None, subagent="general-purpose") -> dict:
    return {
        "ts": datetime.now(UTC).isoformat(),
        "event": "agent_dispatched",
        "model_param": model,
        "subagent_type": subagent,
    }


def _hint(intent: str) -> dict:
    return {"ts": datetime.now(UTC).isoformat(), "event": "model_hint",
            "intent": intent, "model": "sonnet"}


def test_norm_model() -> None:
    assert cr._norm_model("sonnet") == "sonnet"
    assert cr._norm_model("claude-opus-4-8") == "opus"
    assert cr._norm_model("claude-fable-5[1m]") == "fable"
    assert cr._norm_model(None) is None
    assert cr._norm_model("") is None
    assert cr._norm_model("weird") is None


def test_discipline_all_explicit(tmp_path: Path) -> None:
    log = _write_log(tmp_path, [_dispatch("sonnet"), _dispatch("opus"), _dispatch("haiku")])
    r = cr.compute_report(log, since=None, session_model="fable")
    assert r["dispatches"] == 3
    assert r["explicit_model"] == 3
    assert r["inherited"] == 0
    assert r["discipline_pct"] == 100.0
    assert r["by_model"] == {"sonnet": 1, "opus": 1, "haiku": 1}


def test_inherited_counted_and_costed_at_session_tier(tmp_path: Path) -> None:
    # 2 explícitos sonnet + 2 heredados; sesión=fable → heredados cuestan tier fable (5).
    log = _write_log(tmp_path, [_dispatch("sonnet"), _dispatch("sonnet"),
                                _dispatch(None), _dispatch(None)])
    r = cr.compute_report(log, since=None, session_model="claude-fable-5")
    assert r["inherited"] == 2
    assert r["discipline_pct"] == 50.0
    assert r["session_model"] == "fable"
    # costo = 2*sonnet(1) + 2*fable(5) = 12
    assert r["cost_units_relative"] == 12.0


def test_intent_distribution(tmp_path: Path) -> None:
    log = _write_log(tmp_path, [_hint("decision"), _hint("decision"), _hint("simple")])
    r = cr.compute_report(log, since=None)
    assert r["by_intent"] == {"decision": 2, "simple": 1}


def test_empty_log_is_safe(tmp_path: Path) -> None:
    log = tmp_path / "none.jsonl"
    r = cr.compute_report(log, since=None)
    assert r["dispatches"] == 0
    assert r["discipline_pct"] == 0.0


def test_format_report_mentions_discipline(tmp_path: Path) -> None:
    log = _write_log(tmp_path, [_dispatch("sonnet"), _dispatch(None)])
    r = cr.compute_report(log, since=None, session_model="fable")
    out = cr.format_report(r)
    assert "DISCIPLINA DE ROUTING" in out
    assert "heredaron el hilo" in out


def test_main_human_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """main() sin --json emite texto legible con las secciones clave."""
    log = _write_log(tmp_path, [_dispatch("sonnet"), _dispatch("opus"), _dispatch(None)])
    code = cr.main(["--all", "--log", str(log)])
    assert code == 0
    out = capsys.readouterr().out
    assert "ARIS4U ROUTING" in out
    assert "DISCIPLINA DE ROUTING" in out
    # 2 explicit + 1 inherited
    assert "explícito: 2" in out
