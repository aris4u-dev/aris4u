"""Tests del medidor de uplift del enrutador (tools/capability_hint_report.py)."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools import capability_hint_report as r

_EV = [
    {
        "event": "capability_hint", "ts": "2026-06-22T10:00:00+00:00",
        "hinted": ["aris4u.aris_recall_client", "researcher", "aris-council"],
    },
    {"event": "mcp_tool", "ts": "2026-06-22T10:05:00+00:00", "tool": "aris_recall_client"},
    {"event": "subagent_start", "ts": "2026-06-22T10:06:00+00:00", "subagent_type": "researcher"},
]


def test_mcp_and_agent_used_after_counted() -> None:
    bc = r.report(_EV)["by_capability"]
    assert bc["aris4u.aris_recall_client"]["used_after"] == 1  # tool MCP usado después
    assert bc["researcher"]["used_after"] == 1                 # agente usado después


def test_skill_is_unmeasurable() -> None:
    bc = r.report(_EV)["by_capability"]
    assert bc["aris-council"]["measurable"] is False  # skill: sin telemetría de invocación


def test_use_before_hint_not_counted() -> None:
    ev = [
        {"event": "capability_hint", "ts": "2026-06-22T10:00:00+00:00", "hinted": ["aris4u.aris_search"]},
        {"event": "mcp_tool", "ts": "2026-06-22T09:00:00+00:00", "tool": "aris_search"},  # ANTES del hint
    ]
    assert r.report(ev)["by_capability"]["aris4u.aris_search"]["used_after"] == 0


def test_hint_without_ts_is_flagged() -> None:
    ev = [{"event": "capability_hint", "hinted": ["aris4u.aris_search"]}]  # sin ts
    d = r.report(ev)
    assert d["hints_without_ts"] == 1
    assert d["data_sufficiency"] == "low"


def test_load_events_missing_is_empty(tmp_path: Path) -> None:
    assert r.load_events(tmp_path / "noexiste.jsonl") == []


def test_live_report_runs() -> None:
    d = r.report(r.load_events())
    assert "total_hints" in d and "by_capability" in d


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
