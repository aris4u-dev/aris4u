"""Characterization + refactor-safety tests for tools/freeze_report.py.

Pins the EXACT stdout of ``cmd_report`` across every branch (empty window,
populated window, A/B breakdown, per-client tally, vacancy diagnostics, empty
queries, and the primary human-marked metric). These tests are the safety net
for the CC-reduction refactor of ``cmd_report`` and must stay green against the
behavior of the current code.

``tools/`` is not a package, so it is added to sys.path the same way the sibling
tool tests do.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# tools/ is not a package — add it to sys.path like the sibling tool tests do.
_TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import freeze_report  # noqa: E402


def _conn() -> sqlite3.Connection:
    """Open an in-memory sessions.db with the recall_feedback table.

    Returns:
        An sqlite3 connection mirroring what ``_feedback_db`` provisions.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recall_feedback ("
        "recall_id TEXT PRIMARY KEY, useful INTEGER NOT NULL, "
        "marked_at TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def _mark(conn: sqlite3.Connection, recall_id: str, useful: int) -> None:
    """Insert a marked-utility row into recall_feedback."""
    conn.execute(
        "INSERT INTO recall_feedback (recall_id, useful, marked_at) VALUES (?,?,?)",
        (recall_id, useful, "2026-06-19T00:00:00+00:00"),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Branch: no events in the window (early return)
# ---------------------------------------------------------------------------

def test_report_empty_window(capsys: pytest.CaptureFixture[str]) -> None:
    """With zero events the report prints the header and the empty-window note."""
    conn = _conn()
    freeze_report.cmd_report([], conn, 7)
    out = capsys.readouterr().out
    assert out == (
        "\n=== REPORTE FREEZE · últimos 7 días · 0 auto_recalls ===\n"
        "Sin eventos en la ventana. ¿Hubo sesiones? (revisar SessionStart/hook)\n"
    )


# ---------------------------------------------------------------------------
# Branch: populated window — full exact output
# ---------------------------------------------------------------------------

def test_report_populated_exact_output(capsys: pytest.CaptureFixture[str]) -> None:
    """A representative populated window pins the full stdout exactly."""
    events = [
        {"recall_id": "r1", "results": 2, "format": "condensed", "client": "client-c",
         "n_semantic": 3, "latency_ms": 100, "query": "que decidimos sobre RLS"},
        {"recall_id": "r2", "results": 0, "format": "condensed", "client": "client-c",
         "n_semantic": 0, "latency_ms": 200, "query": "tema sin match alguno"},
        {"recall_id": "r3", "results": 1, "format": "raw", "client": "",
         "n_semantic": 5, "latency_ms": 300, "query": "otra cosa"},
    ]
    conn = _conn()
    _mark(conn, "r1", 1)
    _mark(conn, "r3", 0)

    freeze_report.cmd_report(events, conn, 7)
    out = capsys.readouterr().out

    expected = (
        "\n=== REPORTE FREEZE · últimos 7 días · 3 auto_recalls ===\n"
        "Con resultados: 2/3 (66%) · vacíos: 1 (33%)\n"
        "Latencia p50: 200 ms\n"
        "\n-- A/B de formato (crudo vs condensado) --\n"
        "  condensed :    2 recalls · con-resultados 1 (50%)\n"
        "  raw       :    1 recalls · con-resultados 1 (100%)\n"
        "\n-- Por cliente --\n"
        "  client-c            : 2\n"
        "  (sin cliente)       : 1\n"
        "\n-- Diagnóstico del vacío -- (eventos con desglose: 3)\n"
        "  n_semantic==0 (Ollama/embeddings no aportó): 1/3 (33%)\n"
        "\n-- Top queries que salieron VACÍAS (muestra) --\n"
        "  (1x) tema sin match alguno\n"
        "\n=== MÉTRICA PRIMARIA: recalls ÚTILES (marcado humano) ===\n"
        "  útiles: 1 · no-útiles: 1 · sin marcar: 1\n"
        "  útiles/semana ≈ 1.0  (umbral éxito >=3 sostenido 2 sem; <1 = re-diagnosticar)\n"
        "  → 1 sin marcar. Corre: python3 tools/freeze_report.py --review\n"
    )
    assert out == expected


# ---------------------------------------------------------------------------
# Branch: no n_semantic key anywhere → no breakdown section
# ---------------------------------------------------------------------------

def test_report_no_breakdown_no_empty_queries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Events lacking n_semantic and with no empty queries skip those sections."""
    events = [
        {"recall_id": "a", "results": 4, "format": "legacy", "client": "client-b",
         "latency_ms": 50, "query": "algo util"},
        {"recall_id": "b", "results": 1, "format": "legacy", "client": "client-b",
         "latency_ms": 70, "query": "otro util"},
    ]
    conn = _conn()
    freeze_report.cmd_report(events, conn, 14)
    out = capsys.readouterr().out

    expected = (
        "\n=== REPORTE FREEZE · últimos 14 días · 2 auto_recalls ===\n"
        "Con resultados: 2/2 (100%) · vacíos: 0 (0%)\n"
        "Latencia p50: 60 ms\n"
        "\n-- A/B de formato (crudo vs condensado) --\n"
        "  legacy    :    2 recalls · con-resultados 2 (100%)\n"
        "\n-- Por cliente --\n"
        "  client-b            : 2\n"
        "\n=== MÉTRICA PRIMARIA: recalls ÚTILES (marcado humano) ===\n"
        "  útiles: 0 · no-útiles: 0 · sin marcar: 2\n"
        "  útiles/semana ≈ 0.0  (umbral éxito >=3 sostenido 2 sem; <1 = re-diagnosticar)\n"
        "  → 2 sin marcar. Corre: python3 tools/freeze_report.py --review\n"
    )
    assert out == expected


# ---------------------------------------------------------------------------
# Branch: all recalls marked → no "sin marcar" trailing nudge
# ---------------------------------------------------------------------------

def test_report_all_marked_no_nudge(capsys: pytest.CaptureFixture[str]) -> None:
    """When every recall_id is marked, the trailing review nudge is omitted."""
    events = [
        {"recall_id": "x", "results": 3, "format": "condensed", "client": "client-d",
         "n_semantic": 2, "latency_ms": 120, "query": "q1"},
    ]
    conn = _conn()
    _mark(conn, "x", 1)
    freeze_report.cmd_report(events, conn, 7)
    out = capsys.readouterr().out

    expected = (
        "\n=== REPORTE FREEZE · últimos 7 días · 1 auto_recalls ===\n"
        "Con resultados: 1/1 (100%) · vacíos: 0 (0%)\n"
        "Latencia p50: 120 ms\n"
        "\n-- A/B de formato (crudo vs condensado) --\n"
        "  condensed :    1 recalls · con-resultados 1 (100%)\n"
        "\n-- Por cliente --\n"
        "  client-d            : 1\n"
        "\n-- Diagnóstico del vacío -- (eventos con desglose: 1)\n"
        "  n_semantic==0 (Ollama/embeddings no aportó): 0/1 (0%)\n"
        "\n=== MÉTRICA PRIMARIA: recalls ÚTILES (marcado humano) ===\n"
        "  útiles: 1 · no-útiles: 0 · sin marcar: 0\n"
        "  útiles/semana ≈ 1.0  (umbral éxito >=3 sostenido 2 sem; <1 = re-diagnosticar)\n"
    )
    assert out == expected


# ---------------------------------------------------------------------------
# Helper-level pin (defends extracted helpers): latency median
# ---------------------------------------------------------------------------

def test_p50_latency_empty_and_values() -> None:
    """_p50_latency returns 0 for no data and the integer median otherwise."""
    assert freeze_report._p50_latency([]) == 0
    assert freeze_report._p50_latency(
        [{"latency_ms": 10}, {"latency_ms": 30}, {"latency_ms": 20}]
    ) == 20
