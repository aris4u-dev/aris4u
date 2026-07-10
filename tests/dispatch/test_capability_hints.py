"""Tests del router en el hook UserPromptSubmit (_append_capability_hints, paso 4).

Usa el catálogo real (data/capability_triggers.json) + redirige la telemetría a tmp.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from dispatch.events import user_prompt_submit as ups  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_events(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_EVENTS_LOG", str(log))
    monkeypatch.delenv("ARIS4U_ROUTER", raising=False)
    return log


def test_hint_appended_on_match() -> None:
    parts: list[str] = []
    ups._append_capability_hints(
        parts, "necesito decidir la arquitectura, hay trade-offs", "decision", ""
    )
    blob = "\n".join(parts).lower()
    assert "aris-council" in blob


def test_no_hint_on_greeting() -> None:
    parts: list[str] = []
    ups._append_capability_hints(parts, "hola, buenos días", "simple", "")
    assert parts == []


def test_disabled_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_ROUTER", "0")
    parts: list[str] = []
    ups._append_capability_hints(parts, "decidir arquitectura trade-off", "decision", "")
    assert parts == []


def test_telemetry_capability_hint_logged(_tmp_events: Path) -> None:
    parts: list[str] = []
    ups._append_capability_hints(
        parts, "necesito decidir la arquitectura, hay trade-offs", "decision", ""
    )
    events = [json.loads(x) for x in _tmp_events.read_text().splitlines() if x.strip()]
    hint_events = [e for e in events if e.get("event") == "capability_hint"]
    assert hint_events
    assert "aris-council" in hint_events[0]["hinted"]


def test_failopen_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Si route() explota, el hook NO debe romperse (fail-open).
    monkeypatch.setattr(ups, "_log_event", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError()))
    parts: list[str] = []
    ups._append_capability_hints(parts, "decidir arquitectura trade-off", "decision", "")
    # no excepción propagada = pass


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
