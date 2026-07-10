"""Tests del gobernador de concurrencia (núcleo puro decide(), sin I/O)."""

from __future__ import annotations

import json
from pathlib import Path

from engine.v16.orchestration import concurrency_governor as gov


def test_hold_on_high_swap() -> None:
    d = gov.decide(avail_gb=20.0, swap_mb=500.0, cores=18)
    assert d.hold
    assert all(v == 0 for v in d.safe_by_profile.values())


def test_hold_on_low_available_ram() -> None:
    d = gov.decide(avail_gb=5.0, swap_mb=0.0, cores=18)
    assert d.hold  # 5 < margen 8


def test_reasoning_capped_by_harness() -> None:
    d = gov.decide(avail_gb=26.0, swap_mb=0.0, cores=18)
    assert not d.hold
    assert d.harness_cap == 16
    assert d.safe_by_profile["reasoning"] == 16  # min(16, (26-8)/0.35=51)


def test_per_agent_model_is_ram_limited() -> None:
    d = gov.decide(avail_gb=26.0, swap_mb=0.0, cores=18)
    assert d.safe_by_profile["per-agent-model"] == 3  # int((26-8)/5)
    assert d.safe_by_profile["build-test"] == 12  # int(18/1.5)


def test_reasoning_never_below_per_agent_model() -> None:
    for avail in (10.0, 16.0, 26.0, 40.0):
        d = gov.decide(avail_gb=avail, swap_mb=0.0, cores=18)
        assert d.safe_by_profile["reasoning"] >= d.safe_by_profile["per-agent-model"]


def test_low_ram_shrinks_reasoning_below_harness() -> None:
    d = gov.decide(avail_gb=9.0, swap_mb=0.0, cores=18)  # usable=1 GB
    assert not d.hold
    assert d.safe_by_profile["reasoning"] == 2  # min(16, int(1/0.35))
    assert d.safe_by_profile["per-agent-model"] == 0  # no cabe un 7B


def test_harness_cap_override() -> None:
    d = gov.decide(avail_gb=48.0, swap_mb=0.0, cores=18, harness_cap=4)
    assert d.safe_by_profile["reasoning"] == 4


def test_format_hold_and_normal() -> None:
    hold = gov.decide(avail_gb=4.0, swap_mb=0.0, cores=18)
    assert "HOLD" in gov.format_decision(hold)
    ok = gov.decide(avail_gb=26.0, swap_mb=0.0, cores=18)
    report = gov.format_decision(ok, mu_s=397.0, n=39)
    assert "reasoning" in report and "SEGURO" in report


def _write_agent(d: Path, aid: str, start: str, end: str) -> None:
    (d / f"agent-{aid}.jsonl").write_text(
        json.dumps({"timestamp": start}) + "\n" + json.dumps({"timestamp": end}) + "\n",
        encoding="utf-8",
    )


def test_record_and_read_durations(tmp_path: Path) -> None:
    sub = tmp_path / "subagents"
    sub.mkdir()
    _write_agent(sub, "aaa", "2026-07-01T10:00:00.000Z", "2026-07-01T10:05:00.000Z")  # 300s
    _write_agent(sub, "bbb", "2026-07-01T11:00:00.000Z", "2026-07-01T11:02:00.000Z")  # 120s
    log = tmp_path / "dur.jsonl"
    assert gov.record_durations(str(sub), str(log)) == 2
    assert gov.record_durations(str(sub), str(log)) == 0  # idempotente (dedup)
    mu, n = gov.read_recent_mu(str(log))
    assert n == 2
    assert mu == 210.0  # (300+120)/2


def test_read_recent_mu_missing_log() -> None:
    assert gov.read_recent_mu("/nonexistent/xyz.jsonl") == (0.0, 0)


def test_oneline_includes_batch_estimate() -> None:
    d = gov.decide(avail_gb=26.0, swap_mb=0.0, cores=18)
    assert "tanda" in gov.format_oneline(d, mu_s=360.0)
    assert "tanda" not in gov.format_oneline(d, mu_s=0.0)


def test_parse_ram_report_valid() -> None:
    text = "Total 48 GB · load 1.3 · swap 0.00M\n>> DISPONIBLE (libre+caché) = 27.2 GB"
    assert gov._parse_ram_report(text) == (27.2, 0.0)


def test_parse_ram_report_garbage_returns_none() -> None:
    # un fallo de parseo NO debe disfrazarse de HOLD: devuelve None → caller fail-open
    assert gov._parse_ram_report("basura sin formato reconocible") is None
