"""Tests del tool de activación PHI en aris_config (set_healthcare)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import aris_config as ac


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: dict) -> Path:
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"env": env}, indent=2))
    monkeypatch.setattr(ac, "SETTINGS", s)
    return s


def test_set_healthcare_on_writes_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path, monkeypatch, {})
    msg = ac.set_healthcare(True)
    assert "ON" in msg
    assert json.loads(s.read_text())["env"]["ARIS4U_HEALTHCARE"] == "1"


def test_set_healthcare_off_removes_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path, monkeypatch, {"ARIS4U_HEALTHCARE": "1"})
    msg = ac.set_healthcare(False)
    assert "OFF" in msg
    assert "ARIS4U_HEALTHCARE" not in json.loads(s.read_text()).get("env", {})


def test_collect_reports_phi_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(tmp_path, monkeypatch, {"ARIS4U_HEALTHCARE": "1"})
    data = ac.collect()
    assert data["env"]["ARIS4U_HEALTHCARE"] == "1"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
