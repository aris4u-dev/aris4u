"""Tests del etiquetado sin fricción del amplificador F1 (f1_label)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import f1_label  # noqa: E402


def _call(call_id: str | None, tool: str = "aris_structure", available: bool = True,
          ts: str = "2026-06-20T10:00:00+00:00") -> dict:
    return {"event": "mcp_tool", "tool": tool, "call_id": call_id,
            "available": available, "ts": ts, "backend": "mlx", "chars": 500}


def test_pending_excludes_labeled_unavailable_and_noid() -> None:
    """Pendientes = F1 disponibles, con call_id, sin feedback previo."""
    events = [
        _call("a", ts="2026-06-20T10:00:00+00:00"),
        _call("b", ts="2026-06-20T11:00:00+00:00"),          # más reciente
        _call("c", available=False),                          # frío → excluido
        _call(None),                                          # sin call_id → excluido
        {"event": "f1_feedback", "call_id": "a", "useful": True},  # 'a' ya etiquetado
    ]
    pend = f1_label.pending_calls(events)
    ids = [c["call_id"] for c in pend]
    assert ids == ["b"]  # solo 'b'; 'a' etiquetado, 'c' frío, None sin id


def test_pending_newest_first() -> None:
    """El orden es de la más reciente a la más vieja (índice 1 = última llamada)."""
    events = [_call("old", ts="2026-06-20T08:00:00+00:00"),
              _call("new", ts="2026-06-20T12:00:00+00:00")]
    assert [c["call_id"] for c in f1_label.pending_calls(events)] == ["new", "old"]


def test_label_by_index_writes_feedback(tmp_path: Path) -> None:
    """Etiquetar por índice resuelve el call_id y escribe el feedback (a un log tmp)."""
    events = [_call("x", ts="2026-06-20T09:00:00+00:00"),
              _call("y", ts="2026-06-20T10:00:00+00:00")]  # y = #1 (más reciente)
    log = tmp_path / "events.jsonl"
    rc = f1_label.label(events, "1", True, "buena", log_path=log)
    assert rc == 0
    written = [json.loads(line) for line in log.read_text().splitlines()]
    assert written[-1]["call_id"] == "y" and written[-1]["useful"] is True


def test_label_index_out_of_range(tmp_path: Path) -> None:
    """Un índice fuera de rango no escribe y devuelve 1."""
    log = tmp_path / "events.jsonl"
    rc = f1_label.label([_call("z")], "9", True, "", log_path=log)
    assert rc == 1 and not log.exists()
