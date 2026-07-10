"""Tests de F1 — amplificador local de I/O (aris_structure / aris_critique).

Cubre: thinking-off en el payload MLX, las políticas mlx-only, y la lógica de las dos
MCP tools con route_local mockeado (sin depender del server MLX vivo).
"""

from __future__ import annotations

import pytest

import integrations.mcp_server as mcp_server
from engine.v16 import model_router
from engine.v16.model_dispatcher import _mlx_payload
from engine.v16.model_router import RouteResult


def _ok(text: str = "OBJETIVO: x") -> RouteResult:
    return RouteResult(
        text=text, backend="mlx", model="27b", ok=True,
        fallback_used=False, latency_ms=1, candidates_tried=1,
    )


def _down() -> RouteResult:
    return RouteResult(
        text=None, backend="", model="", ok=False,
        fallback_used=False, latency_ms=1, candidates_tried=0, error="no_local_model",
    )


@pytest.fixture(autouse=True)
def _silence_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evita escribir en el log real de telemetría durante los tests."""
    monkeypatch.setattr(mcp_server, "_telemetry", lambda *a, **k: None)


class TestMlxThinkingOff:
    def test_enable_thinking_false_injects_kwarg(self) -> None:
        p = _mlx_payload("m", "sys", "hi", {"enable_thinking": False, "num_predict": 700})
        assert p["chat_template_kwargs"] == {"enable_thinking": False}
        assert p["max_tokens"] == 700

    def test_default_has_no_kwarg(self) -> None:
        p = _mlx_payload("m", "", "hi", {})
        assert "chat_template_kwargs" not in p

    def test_thinking_true_has_no_kwarg(self) -> None:
        p = _mlx_payload("m", "", "hi", {"enable_thinking": True})
        assert "chat_template_kwargs" not in p


class TestPolicies:
    def test_amplify_policies_exist_and_mlx_only(self) -> None:
        for task in ("structure_prompt", "critique"):
            assert task in model_router._POLICY
            backends = [c.backend for c in model_router._POLICY[task]]
            assert backends == ["mlx"]  # solo el 27B → fail-open = omitir, no degradar

    def test_amplify_policies_thinking_off(self) -> None:
        for task in ("structure_prompt", "critique"):
            cand = model_router._POLICY[task][0]
            assert cand.options.get("enable_thinking") is False


class TestLooksStructured:
    def test_raw_idea_not_structured(self) -> None:
        assert not mcp_server._looks_structured("quiero un cache para los embeddings")

    def test_sections_detected(self) -> None:
        assert mcp_server._looks_structured("OBJETIVO: x\nREQUISITOS: y\nRIESGOS: z")

    def test_bullets_detected(self) -> None:
        assert mcp_server._looks_structured("- uno\n- dos\n- tres")


class TestArisStructure:
    def test_amplifies_raw_idea(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(model_router, "route_local", lambda *a, **k: _ok())
        out = mcp_server.aris_structure("quiero un cache de embeddings")
        assert "cuerpo local" in out
        assert "OBJETIVO" in out

    def test_passthrough_when_already_structured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"n": 0}

        def spy(*a, **k):  # no debería llamarse
            called["n"] += 1
            return _ok()

        monkeypatch.setattr(model_router, "route_local", spy)
        out = mcp_server.aris_structure("OBJETIVO: x\nREQUISITOS: y\nCRITERIOS: z")
        assert "sin cambios" in out
        assert called["n"] == 0

    def test_omits_when_mlx_cold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # V18: el dead-end local solo aplica en healthcare (fuera de él se delega a Sonnet).
        monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
        monkeypatch.setattr(model_router, "route_local", lambda *a, **k: _down())
        out = mcp_server.aris_structure("idea cruda corta")
        assert "no disponible" in out
        assert "mlx_serve.sh" in out

    def test_delegates_to_sonnet_non_phi_when_mlx_cold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARIS4U_HEALTHCARE", "0")
        monkeypatch.setattr(model_router, "route_local", lambda *a, **k: _down())
        out = mcp_server.aris_structure("idea cruda corta")
        assert 'Agent(model="sonnet"' in out


class TestArisCritique:
    def test_returns_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            model_router, "route_local", lambda *a, **k: _ok("FLAG: división por cero")
        )
        out = mcp_server.aris_critique("def f(): return 1 / 0")
        assert "filtrar" in out  # nota de "sugerencia a filtrar, no veredicto"
        assert "FLAG" in out

    def test_passes_angles_into_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def capture(task, prompt, **k):
            captured["task"] = task
            captured["prompt"] = prompt
            return _ok("ok")

        monkeypatch.setattr(model_router, "route_local", capture)
        mcp_server.aris_critique("código", angles="seguridad, performance")
        assert captured["task"] == "critique"
        assert "seguridad" in captured["prompt"]
        assert "performance" in captured["prompt"]

    def test_omits_when_mlx_cold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # V18: dead-end local solo en healthcare; fuera se delega a Sonnet.
        monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
        monkeypatch.setattr(model_router, "route_local", lambda *a, **k: _down())
        out = mcp_server.aris_critique("algo")
        assert "no disponible" in out

    def test_delegates_to_sonnet_non_phi_when_mlx_cold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARIS4U_HEALTHCARE", "0")
        monkeypatch.setattr(model_router, "route_local", lambda *a, **k: _down())
        out = mcp_server.aris_critique("algo")
        assert 'Agent(model="sonnet"' in out
