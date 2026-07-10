"""Characterization tests for ``tools/analyze_validation_log.py``.

These tests pin the EXACT current behavior of ``analyze()`` before it is
refactored, acting as a safety net (golden-master / characterization style).
They feed the function synthetic JSONL logs written into ``tmp_path`` covering
every branch — empty log, only-invalid-json, events present but no tracked
sections, a rich mixed log, and the p95-quantile path (>= 20 latencies) — and
assert on the precise text printed to stdout and on the return value.

``analyze()`` prints a report and returns ``None``; there is no structured
return to assert on, so stdout (via ``capsys``) is the observable contract.

Import pattern follows the sibling tools tests (``tools/`` is registered on
``sys.path`` by ``tests/conftest.py`` via the project root, and is also a
package; we additionally insert ``tools/`` directly to mirror the logger test).
"""

from __future__ import annotations

import sys
from pathlib import Path

# tools/ is added to sys.path like the sibling tools tests do.
_TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import tools.analyze_validation_log as mod  # noqa: E402


def _write(tmp_path: Path, name: str, lines: list[str]) -> str:
    """Write *lines* as a JSONL file and return its path as a string."""
    p = tmp_path / name
    p.write_text("".join(line + "\n" for line in lines))
    return str(p)


# ---------------------------------------------------------------------------
# Empty / no-events branch
# ---------------------------------------------------------------------------


def test_empty_file_prints_no_events_and_returns_none(tmp_path, capsys) -> None:
    """An empty file prints the no-events line and returns ``None``."""
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    result = mod.analyze(str(p))
    out = capsys.readouterr().out
    assert result is None
    assert out == f"No events found in {p}\n"


def test_only_invalid_json_treated_as_no_events(tmp_path, capsys) -> None:
    """Lines that fail to JSON-decode are skipped, yielding the no-events path."""
    path = _write(tmp_path, "bad.jsonl", ["garbage", "not json", "{nope"])
    result = mod.analyze(path)
    out = capsys.readouterr().out
    assert result is None
    assert out == f"No events found in {path}\n"


# ---------------------------------------------------------------------------
# Events present but none of the tracked event types
# ---------------------------------------------------------------------------


def test_untracked_events_render_all_empty_sections(tmp_path, capsys) -> None:
    """Events of unknown type render every section as 'No ... recorded'."""
    path = _write(
        tmp_path,
        "misc.jsonl",
        ['{"event": "misc", "ts": "t1"}', '{"event": "misc", "ts": "t2"}'],
    )
    result = mod.analyze(path)
    out = capsys.readouterr().out

    assert result is None
    expected = (
        "\n"
        + "=" * 70 + "\n"
        + "ARIS4U V16.1 Validation Log Analysis\n"
        + "=" * 70 + "\n"
        + f"File: {path}\n"
        + "Total events: 2\n"
        + "Timestamp range: t1 → t2\n"
        + "\n"
        + "─" * 70 + "\n"
        + "EVENT COUNTS\n"
        + "─" * 70 + "\n"
        + "  misc                         2\n"
        + "\n"
        + "─" * 70 + "\n"
        + "LATENCY STATS (p50/p95/max ms)\n"
        + "─" * 70 + "\n"
        + "\n"
        + "─" * 70 + "\n"
        + "F5 PREVALIDATION\n"
        + "─" * 70 + "\n"
        + "  No F5 events recorded\n"
        + "\n"
        + "─" * 70 + "\n"
        + "NOVELTY DETECTION\n"
        + "─" * 70 + "\n"
        + "  No novelty events recorded\n"
        + "\n"
        + "─" * 70 + "\n"
        + "AUTOTEST\n"
        + "─" * 70 + "\n"
        + "  No autotest events recorded\n"
        + "\n"
        + "─" * 70 + "\n"
        + "DEPTH VALIDATOR\n"
        + "─" * 70 + "\n"
        + "  No depth validator events recorded\n"
        + "\n"
        + "─" * 70 + "\n"
        + "CONTRACT GUARD\n"
        + "─" * 70 + "\n"
        + "  No contract guard events recorded\n"
        + "\n"
        + "─" * 70 + "\n"
        + "GOAL TRACKING\n"
        + "─" * 70 + "\n"
        + "  No goal tracking events recorded\n"
        + "\n"
        + "─" * 70 + "\n"
        + "VOTING / CONSENSUS\n"
        + "─" * 70 + "\n"
        + "  No voting events recorded\n"
        + "\n"
        + "=" * 70 + "\n"
        + "\n"
    )
    assert out == expected


# ---------------------------------------------------------------------------
# Rich mixed log exercising every populated section
# ---------------------------------------------------------------------------


_MIXED_LINES = [
    '{"event": "f5_prevalidation", "ts": "2026-01-01T00:00:00", "result": "pass", "latency_ms": 10}',
    '{"event": "f5_prevalidation", "ts": "2026-01-01T00:01:00", "result": "fail", "latency_ms": 20}',
    '{"event": "f5_prevalidation", "ts": "2026-01-01T00:02:00", "result": "advisory"}',
    '{"event": "novelty_detection", "ts": "2026-01-01T00:03:00", "is_new_domain": true}',
    '{"event": "novelty_detection", "ts": "2026-01-01T00:04:00", "is_new_domain": false}',
    '{"event": "autotest", "ts": "2026-01-01T00:05:00", "failed": 0}',
    '{"event": "autotest", "ts": "2026-01-01T00:06:00", "failed": 2}',
    '{"event": "depth_validator", "ts": "2026-01-01T00:07:00", "latency_ms": 5}',
    '{"event": "contract_guard", "ts": "2026-01-01T00:08:00", "allowed": false}',
    '{"event": "contract_guard", "ts": "2026-01-01T00:09:00", "allowed": true}',
    '{"event": "goal_checkpoint", "ts": "2026-01-01T00:10:00", "preserved": true}',
    '{"event": "pre_compact", "ts": "2026-01-01T00:11:00", "goal_restored": true}',
    '{"event": "post_compact", "ts": "2026-01-01T00:12:00"}',
    '{"event": "voting", "ts": "2026-01-01T00:13:00", "approved": true}',
    '{"event": "voting", "ts": "2026-01-01T00:14:00", "approved": false}',
    "not valid json line",
]


def test_mixed_log_full_report(tmp_path, capsys) -> None:
    """A representative mixed log renders every section with computed stats."""
    path = _write(tmp_path, "mixed.jsonl", _MIXED_LINES)
    result = mod.analyze(path)
    out = capsys.readouterr().out

    assert result is None
    expected = (
        "\n"
        + "=" * 70 + "\n"
        + "ARIS4U V16.1 Validation Log Analysis\n"
        + "=" * 70 + "\n"
        + f"File: {path}\n"
        + "Total events: 15\n"
        + "Timestamp range: 2026-01-01T00:00:00 → 2026-01-01T00:14:00\n"
        + "\n"
        + "─" * 70 + "\n"
        + "EVENT COUNTS\n"
        + "─" * 70 + "\n"
        + "  f5_prevalidation             3\n"
        + "  novelty_detection            2\n"
        + "  autotest                     2\n"
        + "  contract_guard               2\n"
        + "  voting                       2\n"
        + "  depth_validator              1\n"
        + "  goal_checkpoint              1\n"
        + "  pre_compact                  1\n"
        + "  post_compact                 1\n"
        + "\n"
        + "─" * 70 + "\n"
        + "LATENCY STATS (p50/p95/max ms)\n"
        + "─" * 70 + "\n"
        + "  depth_validator                5 /      5 /      5\n"
        + "  f5_prevalidation              15 /     20 /     20\n"
        + "\n"
        + "─" * 70 + "\n"
        + "F5 PREVALIDATION\n"
        + "─" * 70 + "\n"
        + "  Total validations:   3\n"
        + "  Pass:                1\n"
        + "  Fail:                1\n"
        + "  Advisory:            1\n"
        + "\n"
        + "─" * 70 + "\n"
        + "NOVELTY DETECTION\n"
        + "─" * 70 + "\n"
        + "  Total probes:        2\n"
        + "  New domains:         1\n"
        + "  Known domains:       1\n"
        + "\n"
        + "─" * 70 + "\n"
        + "AUTOTEST\n"
        + "─" * 70 + "\n"
        + "  Total test runs:     2\n"
        + "  Passed:              1\n"
        + "  Failed:              1\n"
        + "  Pass rate:           50.0%\n"
        + "\n"
        + "─" * 70 + "\n"
        + "DEPTH VALIDATOR\n"
        + "─" * 70 + "\n"
        + "  Total validations:   1\n"
        + "\n"
        + "─" * 70 + "\n"
        + "CONTRACT GUARD\n"
        + "─" * 70 + "\n"
        + "  Total checks:        2\n"
        + "  Blocked:             1\n"
        + "  Allowed:             1\n"
        + "\n"
        + "─" * 70 + "\n"
        + "GOAL TRACKING\n"
        + "─" * 70 + "\n"
        + "  Total goal events:   3\n"
        + "  Goals preserved:     2\n"
        + "\n"
        + "─" * 70 + "\n"
        + "VOTING / CONSENSUS\n"
        + "─" * 70 + "\n"
        + "  Total votes:         2\n"
        + "  Approved:            1\n"
        + "  Rejected:            1\n"
        + "\n"
        + "=" * 70 + "\n"
        + "\n"
    )
    assert out == expected


# ---------------------------------------------------------------------------
# Latency p95 quantile branch (>= 20 samples)
# ---------------------------------------------------------------------------


def test_latency_p95_uses_quantiles_when_twenty_or_more(tmp_path, capsys) -> None:
    """With >= 20 latencies, p95 comes from statistics.quantiles, not max."""
    # latencies 1..20 -> median 10.5 (->10 via banker's rounding in %.0f),
    # statistics.quantiles(1..20, n=20)[18] == 20.0, max == 20.
    lines = [
        f'{{"event": "depth_validator", "ts": "t{i}", "latency_ms": {i}}}'
        for i in range(1, 21)
    ]
    path = _write(tmp_path, "lat.jsonl", lines)
    mod.analyze(path)
    out = capsys.readouterr().out

    assert "  depth_validator               10 /     20 /     20\n" in out


def test_latency_below_twenty_uses_max_for_p95(tmp_path, capsys) -> None:
    """With < 20 latencies, p95 falls back to max."""
    lines = [
        f'{{"event": "depth_validator", "ts": "t{i}", "latency_ms": {i}}}'
        for i in range(1, 6)  # 1..5
    ]
    path = _write(tmp_path, "lat5.jsonl", lines)
    mod.analyze(path)
    out = capsys.readouterr().out
    # median(1..5)=3, p95 fallback = max = 5
    assert "  depth_validator                3 /      5 /      5\n" in out
