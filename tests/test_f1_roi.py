"""Tests del harness de ROI de F1 (blueprint F2): f1_roi + f1_feedback."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import f1_feedback  # noqa: E402  (script de tools/, requiere el sys.path de arriba)
import f1_roi  # noqa: E402


def _calls_and_feedback() -> list[dict]:
    return [
        {"event": "mcp_tool", "tool": "aris_structure", "available": True, "latency_ms": 20000, "call_id": "a1"},
        {"event": "mcp_tool", "tool": "aris_critique", "available": True, "latency_ms": 16000, "call_id": "a2"},
        {"event": "mcp_tool", "tool": "aris_structure", "available": False},  # MLX frío
        {"event": "mcp_tool", "tool": "aris_health"},  # no es F1 → se ignora
        {"event": "f1_feedback", "call_id": "a1", "useful": True},
        {"event": "f1_feedback", "call_id": "a2", "useful": False},
    ]


class TestComputeRoi:
    def test_counts_and_rates(self) -> None:
        roi = f1_roi.compute_roi(_calls_and_feedback())
        assert roi["total_calls"] == 3  # health excluido
        assert roi["available"] == 2
        assert roi["unavailable"] == 1
        assert roi["availability_rate"] == pytest.approx(2 / 3)
        assert roi["by_tool"] == {"aris_structure": 2, "aris_critique": 1}
        assert roi["labeled"] == 2
        assert roi["useful"] == 1
        assert roi["useful_rate"] == pytest.approx(0.5)
        assert roi["ready_for_calibration"] is False

    def test_latency_percentiles(self) -> None:
        roi = f1_roi.compute_roi(_calls_and_feedback())
        # latencias disponibles: [20000, 16000] → p50 = 18000
        assert roi["latency_p50_ms"] == pytest.approx(18000)

    def test_empty_events(self) -> None:
        roi = f1_roi.compute_roi([])
        assert roi["total_calls"] == 0
        assert roi["availability_rate"] == 0.0
        assert roi["useful_rate"] == 0.0
        assert roi["ready_for_calibration"] is False

    def test_ready_when_enough_labels(self) -> None:
        events = []
        for i in range(30):
            cid = f"c{i}"
            events.append({"event": "mcp_tool", "tool": "aris_critique", "available": True,
                           "latency_ms": 1000, "call_id": cid})
            events.append({"event": "f1_feedback", "call_id": cid, "useful": i % 2 == 0})
        roi = f1_roi.compute_roi(events)
        assert roi["labeled"] == 30
        assert roi["ready_for_calibration"] is True
        assert roi["useful_rate"] == pytest.approx(0.5, abs=0.02)


class TestPercentile:
    def test_empty(self) -> None:
        assert f1_roi._percentile([], 0.5) == 0.0

    def test_median(self) -> None:
        assert f1_roi._percentile([1.0, 2.0, 3.0], 0.5) == pytest.approx(2.0)


class TestReadEvents:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert f1_roi.read_events(tmp_path / "nope.jsonl") == []

    def test_skips_corrupt_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "ev.jsonl"
        log.write_text('{"a":1}\nNOT JSON\n\n{"b":2}\n')
        assert f1_roi.read_events(log) == [{"a": 1}, {"b": 2}]


class TestFeedback:
    def test_record_writes_event(self, tmp_path: Path) -> None:
        log = tmp_path / "logs" / "ev.jsonl"
        ev = f1_feedback.record_feedback(log, "abc123", True, "ayudó mucho")
        assert ev["call_id"] == "abc123"
        assert ev["useful"] is True
        written = json.loads(log.read_text().strip())
        assert written["event"] == "f1_feedback"
        assert written["note"] == "ayudó mucho"
        # round-trip por el lector
        assert f1_roi.read_events(log) == [written]

    def test_parse_useful_truthy(self) -> None:
        assert f1_feedback.parse_useful("ok") is True
        assert f1_feedback.parse_useful("SÍ") is True

    def test_parse_useful_falsy(self) -> None:
        assert f1_feedback.parse_useful("no") is False

    def test_parse_useful_invalid_raises(self) -> None:
        with pytest.raises(SystemExit):
            f1_feedback.parse_useful("quizá")


def _scored(call_id: str, score: float, useful: bool) -> list[dict]:
    return [{"event": "mcp_tool", "tool": "aris_structure", "available": True,
             "call_id": call_id, "promise_score": score},
            {"event": "f1_feedback", "call_id": call_id, "useful": useful}]


def test_calibration_data_extracts_scored_labeled_pairs() -> None:
    """calibration_data toma solo llamadas etiquetadas CON promise_score."""
    ev = _scored("a", 0.8, True) + _scored("b", 0.3, False)
    ev.append({"event": "mcp_tool", "tool": "aris_structure", "available": True,
               "call_id": "c", "promise_score": 0.5})  # con score pero SIN etiqueta → fuera
    scores, succ = f1_roi.calibration_data(ev)
    assert scores == [0.8, 0.3] and succ == [1.0, 0.0]


def test_run_calibration_insufficient_data() -> None:
    """Con <30 pares scored, la calibración no corre (honesto)."""
    ev = _scored("a", 0.8, True)
    out = f1_roi.run_calibration(ev)
    assert out["ran"] is False and "promise_score" in out["reason"]


def test_run_calibration_fires_with_30_scored() -> None:
    """Con 30 pares (score predice utilidad), sensor_is_predictive corre y dictamina."""
    ev: list[dict] = []
    for i in range(30):
        s = i / 29.0                       # 0..1 monótono
        # útil sii score alto, PERO con 4 'errores' → fuerte sin separación perfecta
        # (la separación perfecta haría diverger la regresión logística).
        useful = (s > 0.5) != (i in (5, 12, 18, 23))
        ev += _scored(f"c{i}", s, useful)
    out = f1_roi.run_calibration(ev)
    assert out["ran"] is True and out["n"] == 30
    assert isinstance(out["predictive"], bool) and out["auc"] > 0.5


def test_promise_from_logprobs() -> None:
    """promise_score = exp(media de logprobs) ∈ (0,1]; None si no hay logprobs."""
    from engine.v16.model_dispatcher import _promise_from_logprobs
    import math
    choice = {"logprobs": {"content": [{"logprob": -0.1}, {"logprob": -0.3}]}}
    assert _promise_from_logprobs(choice) == round(math.exp(-0.2), 4)
    assert _promise_from_logprobs({"message": {"content": "x"}}) is None
