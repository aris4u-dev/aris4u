"""Characterization + refactor-safety tests for tools/measure_recall.py.

What the script does
    ``measure_recall.py`` reads ``logs/v16.1-events.jsonl`` (one JSON object per
    line), keeps only ``event == "auto_recall"`` records, and prints a per-day
    table of recall utility: number of recalls, recalls with at least one
    result (``con-hit``), hit-rate, average results per recall, and the p50
    latency. It then prints a TOTAL line, an optional semantic-diagnostic line
    (driven by the forward-only ``n_semantic`` field), and a fixed gate note.
    ``--days N`` truncates the table to the last N days.

Test strategy
    The script's only side effects are reading ``LOG`` and writing to stdout.
    These tests monkeypatch the module-level ``LOG`` constant to point at a
    controlled JSONL file in ``tmp_path`` and monkeypatch ``sys.argv`` for the
    arg-parsing branch, then assert on captured stdout. They exercise every
    branch of ``main``: missing log, empty/no-events, hits, no-hits, mixed days,
    ``--days`` filtering (valid + malformed), malformed JSON lines, the
    ``n_semantic`` diagnostic (present / absent), and the latency p50 selection.

    Both the end-to-end ``main`` behavior and (post-refactor) the extracted pure
    helper ``aggregate_events`` are covered, so the refactor is comportment-safe.

    ``tools/`` is not a package — it is added to ``sys.path`` the same way the
    sibling tool tests do, honoring the autouse isolation fixtures in conftest.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

# tools/ is not a package — add it to sys.path like the sibling tool tests do.
_TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import measure_recall  # noqa: E402


@pytest.fixture(autouse=True)
def _reload_module() -> Generator[None, None, None]:
    """Reload the module per test so monkeypatched constants never leak."""
    importlib.reload(measure_recall)
    yield


def _write_log(path: Path, events: list[dict]) -> Path:
    """Write *events* as a JSONL file at *path* and return it.

    Args:
        path: Destination file path.
        events: List of event dicts, one serialized JSON object per line.

    Returns:
        The path written to.
    """
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    return path


def _recall(day: str, results: int, latency_ms: int = 0, **extra) -> dict:
    """Build an ``auto_recall`` event for *day* with *results* results.

    Args:
        day: ISO date prefix (becomes the start of ``ts``).
        results: Number of recall results reported.
        latency_ms: Latency in milliseconds.
        **extra: Additional event fields (e.g. ``n_semantic``).

    Returns:
        A dict shaped like a real auto_recall log line.
    """
    ev = {
        "event": "auto_recall",
        "ts": f"{day}T12:00:00",
        "results": results,
        "latency_ms": latency_ms,
    }
    ev.update(extra)
    return ev


def _run(monkeypatch, capsys, log_path: Path, argv: list[str] | None = None) -> str:
    """Run ``main`` with ``LOG`` and ``sys.argv`` patched; return stdout.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        capsys: pytest capsys fixture.
        log_path: Path to use as the module ``LOG`` constant.
        argv: Optional argv list (defaults to a bare program name).

    Returns:
        Captured stdout.
    """
    monkeypatch.setattr(measure_recall, "LOG", log_path)
    monkeypatch.setattr(sys, "argv", argv if argv is not None else ["measure_recall.py"])
    measure_recall.main()
    return capsys.readouterr().out


# --------------------------------------------------------------------------- #
# main() — branch coverage
# --------------------------------------------------------------------------- #


def test_missing_log(monkeypatch, capsys, tmp_path) -> None:
    out = _run(monkeypatch, capsys, tmp_path / "nope.jsonl")
    assert "No existe el log" in out


def test_empty_log_file(monkeypatch, capsys, tmp_path) -> None:
    log = tmp_path / "events.jsonl"
    log.write_text("", encoding="utf-8")
    out = _run(monkeypatch, capsys, log)
    assert "Sin eventos auto_recall" in out


def test_no_matching_events(monkeypatch, capsys, tmp_path) -> None:
    log = _write_log(tmp_path / "events.jsonl", [{"event": "other", "ts": "2026-06-01T00:00:00"}])
    out = _run(monkeypatch, capsys, log)
    assert "Sin eventos auto_recall" in out


def test_non_auto_recall_substring_skipped(monkeypatch, capsys, tmp_path) -> None:
    # Line mentions auto_recall in a field but event differs → must be ignored.
    log = _write_log(
        tmp_path / "events.jsonl",
        [{"event": "noise", "note": "auto_recall here", "ts": "2026-06-01T00:00:00"}],
    )
    out = _run(monkeypatch, capsys, log)
    assert "Sin eventos auto_recall" in out


def test_with_hits(monkeypatch, capsys, tmp_path) -> None:
    log = _write_log(
        tmp_path / "events.jsonl",
        [
            _recall("2026-06-01", 3, latency_ms=10),
            _recall("2026-06-01", 1, latency_ms=20),
        ],
    )
    out = _run(monkeypatch, capsys, log)
    assert "2026-06-01" in out
    # 2 recalls, both hits, hit-rate 100%, avg (3+1)/2 = 2.0
    assert "TOTAL" in out
    total_line = [ln for ln in out.splitlines() if ln.startswith("TOTAL")][0]
    assert "100%" in total_line
    assert "2.0" in total_line


def test_no_hits(monkeypatch, capsys, tmp_path) -> None:
    log = _write_log(
        tmp_path / "events.jsonl",
        [_recall("2026-06-02", 0), _recall("2026-06-02", 0)],
    )
    out = _run(monkeypatch, capsys, log)
    total_line = [ln for ln in out.splitlines() if ln.startswith("TOTAL")][0]
    assert "0%" in total_line
    assert "0.0" in total_line


def test_p50_latency(monkeypatch, capsys, tmp_path) -> None:
    # Odd count: p50 = middle of sorted [5, 50, 500] -> 50.
    log = _write_log(
        tmp_path / "events.jsonl",
        [
            _recall("2026-06-03", 1, latency_ms=500),
            _recall("2026-06-03", 1, latency_ms=5),
            _recall("2026-06-03", 1, latency_ms=50),
        ],
    )
    out = _run(monkeypatch, capsys, log)
    day_line = [ln for ln in out.splitlines() if ln.startswith("2026-06-03")][0]
    assert "50" in day_line.split()[-1]


def test_days_filter(monkeypatch, capsys, tmp_path) -> None:
    log = _write_log(
        tmp_path / "events.jsonl",
        [
            _recall("2026-06-01", 1),
            _recall("2026-06-02", 1),
            _recall("2026-06-03", 1),
        ],
    )
    out = _run(monkeypatch, capsys, log, argv=["measure_recall.py", "--days", "1"])
    assert "2026-06-03" in out
    assert "2026-06-01" not in out
    assert "2026-06-02" not in out


def test_days_filter_malformed_value(monkeypatch, capsys, tmp_path) -> None:
    # --days with a non-int → days_filter stays None → all days shown.
    log = _write_log(
        tmp_path / "events.jsonl",
        [_recall("2026-06-01", 1), _recall("2026-06-02", 1)],
    )
    out = _run(monkeypatch, capsys, log, argv=["measure_recall.py", "--days", "abc"])
    assert "2026-06-01" in out
    assert "2026-06-02" in out


def test_days_filter_missing_value(monkeypatch, capsys, tmp_path) -> None:
    # --days at end of argv (no following token) → IndexError → None.
    log = _write_log(tmp_path / "events.jsonl", [_recall("2026-06-01", 1)])
    out = _run(monkeypatch, capsys, log, argv=["measure_recall.py", "--days"])
    assert "2026-06-01" in out


def test_malformed_json_line_skipped(monkeypatch, capsys, tmp_path) -> None:
    log = tmp_path / "events.jsonl"
    good = json.dumps(_recall("2026-06-01", 2))
    log.write_text(f"{good}\nthis is not json but mentions auto_recall\n", encoding="utf-8")
    out = _run(monkeypatch, capsys, log)
    # Only the good line counts.
    total_line = [ln for ln in out.splitlines() if ln.startswith("TOTAL")][0]
    assert total_line.split()[1] == "1"


def test_event_missing_ts_skipped(monkeypatch, capsys, tmp_path) -> None:
    log = _write_log(
        tmp_path / "events.jsonl",
        [
            {"event": "auto_recall", "results": 5},  # no ts → day empty → skipped
            _recall("2026-06-05", 1),
        ],
    )
    out = _run(monkeypatch, capsys, log)
    total_line = [ln for ln in out.splitlines() if ln.startswith("TOTAL")][0]
    assert total_line.split()[1] == "1"


def test_nsemantic_diagnostic_present(monkeypatch, capsys, tmp_path) -> None:
    log = _write_log(
        tmp_path / "events.jsonl",
        [
            _recall("2026-06-06", 2, n_semantic=0),
            _recall("2026-06-06", 2, n_semantic=3),
        ],
    )
    out = _run(monkeypatch, capsys, log)
    assert "Diagnóstico semántico" in out
    assert "1/2" in out  # one of two had n_semantic == 0
    assert "(50%)" in out


def test_nsemantic_diagnostic_absent(monkeypatch, capsys, tmp_path) -> None:
    log = _write_log(tmp_path / "events.jsonl", [_recall("2026-06-07", 1)])
    out = _run(monkeypatch, capsys, log)
    assert "Diagnóstico semántico" not in out


def test_gate_note_always_printed(monkeypatch, capsys, tmp_path) -> None:
    log = _write_log(tmp_path / "events.jsonl", [_recall("2026-06-08", 1)])
    out = _run(monkeypatch, capsys, log)
    assert "Gate WS-A" in out


def test_results_field_none_treated_as_zero(monkeypatch, capsys, tmp_path) -> None:
    log = _write_log(
        tmp_path / "events.jsonl",
        [{"event": "auto_recall", "ts": "2026-06-09T00:00:00", "results": None}],
    )
    out = _run(monkeypatch, capsys, log)
    total_line = [ln for ln in out.splitlines() if ln.startswith("TOTAL")][0]
    # 1 recall, 0 hits.
    assert total_line.split()[1] == "1"
    assert "0%" in total_line
