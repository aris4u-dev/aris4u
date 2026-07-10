"""Tests del driver SkillOpt (tools/skillopt_propose.py).

Verifican propose/validate de forma DETERMINISTA: `route_local` (MLX) se
monkeypatchea, así no se depende de que el modelo local este caliente. El
verificador sigue siendo el linter real (migration_linter, exit 0/1).
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

import engine.v16.model_router as model_router

propose = importlib.import_module("tools.skillopt_propose")

# Marca de regla que el agente demo busca para emitir SQL limpio (= la del driver).
RULE = "now() en el predicado where de un indice parcial"


def _fake_route(ok: bool, text: str = "") -> Any:
    def _route(task: str, prompt: str, **kw: Any) -> SimpleNamespace:
        del task, prompt, kw
        return SimpleNamespace(ok=ok, text=text)
    return _route


def test_propose_emits_proposal_with_mlx_flags(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(model_router, "route_local", _fake_route(True, "- regla A\n- regla B"))
    skill = tmp_path / "SKILL.md"
    skill.write_text("skill sin la regla", encoding="utf-8")

    rc = propose.cmd_propose(str(skill))
    out = skill.with_suffix(".skillopt-proposal.md")

    assert rc == 0
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "Score base" in body and "0.000" in body  # sin regla -> SQL buggy -> 0
    assert "regla A" in body and "regla B" in body    # flags de MLX incorporados


def test_propose_fail_open_when_mlx_cold(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(model_router, "route_local", _fake_route(False))
    skill = tmp_path / "SKILL.md"
    skill.write_text("skill sin la regla", encoding="utf-8")

    rc = propose.cmd_propose(str(skill))
    body = skill.with_suffix(".skillopt-proposal.md").read_text(encoding="utf-8")

    assert rc == 0
    assert "MLX frio" in body  # fail-open: no degrada, reporta crudo


def test_validate_accepts_candidate_that_improves(tmp_path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("skill sin la regla", encoding="utf-8")
    cand = tmp_path / "cand.md"
    cand.write_text("skill mejorado.\n" + RULE, encoding="utf-8")  # trae la regla

    rc = propose.cmd_validate(str(skill), str(cand))
    assert rc == 0  # ACCEPT: el candidato mejora estricto en held-out


def test_validate_rejects_candidate_that_does_not_improve(tmp_path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("skill sin la regla", encoding="utf-8")
    cand = tmp_path / "cand.md"
    cand.write_text("otro texto inutil sin la regla", encoding="utf-8")

    rc = propose.cmd_validate(str(skill), str(cand))
    assert rc == 1  # REJECT: no supera el score base


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
