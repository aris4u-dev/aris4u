"""Tests del reporte de hit-rate de adopción (tools/conductor_stats.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import conductor_stats as cs  # noqa: E402


def _write_log(tmp_path: Path, events: list[dict]) -> Path:
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return p


def test_report_computes_hit_rate(tmp_path: Path) -> None:
    events = [
        {"event": "capability_hint", "hinted": ["aris-council"], "intent": "decision"},
        {"event": "capability_adopted", "name": "aris-council", "intent": "decision"},
        {"event": "capability_hint", "hinted": ["status"], "intent": "decision"},
        {"event": "capability_ignored", "name": "status", "intent": "decision"},
        {"event": "capability_adopted", "name": "aris-council", "intent": "implementation"},
    ]
    data = cs.report(cs.load_events(_write_log(tmp_path, events)))
    assert data["total_hints"] == 2
    assert data["total_adopted"] == 2
    assert data["total_ignored"] == 1
    assert data["resolved"] == 3
    assert data["overall_rate"] == round(2 / 3, 3)
    assert data["by_capability"]["aris-council"]["rate"] == 1.0
    assert data["by_capability"]["status"]["rate"] == 0.0
    assert data["by_intent"]["decision"]["adopted"] == 1
    assert data["by_intent"]["decision"]["ignored"] == 1


def test_low_sufficiency_flagged(tmp_path: Path) -> None:
    data = cs.report(cs.load_events(_write_log(tmp_path, [
        {"event": "capability_adopted", "name": "x", "intent": "decision"},
    ])))
    assert data["data_sufficiency"] == "low"


def test_render_runs(tmp_path: Path) -> None:
    data = cs.report(cs.load_events(_write_log(tmp_path, [
        {"event": "capability_adopted", "name": "aris-council", "intent": "decision"},
        {"event": "capability_ignored", "name": "status", "intent": "decision"},
    ])))
    out = cs.render(data)
    assert "HIT-RATE DE ADOPCIÓN" in out
    assert "aris-council" in out


def test_missing_log_failopen(tmp_path: Path) -> None:
    data = cs.report(cs.load_events(tmp_path / "nope.jsonl"))
    assert data["resolved"] == 0
    assert data["overall_rate"] is None


def test_main_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log = _write_log(tmp_path, [
        {"event": "capability_adopted", "name": "aris-council", "intent": "decision"},
    ])
    assert cs.main(["--json", "--log", str(log)]) == 0
    assert json.loads(capsys.readouterr().out)["total_adopted"] == 1


def test_main_human_render(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """main() sin --json produce texto legible con secciones clave."""
    log = _write_log(tmp_path, [
        {"event": "capability_adopted", "name": "aris-council", "intent": "decision"},
        {"event": "capability_ignored", "name": "status", "intent": "decision"},
    ])
    assert cs.main(["--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "HIT-RATE DE ADOPCIÓN" in out
    assert "aris-council" in out
    assert "status" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
