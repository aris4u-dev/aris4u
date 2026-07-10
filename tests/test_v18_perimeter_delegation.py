"""V18 Fase B — perímetro reconvertido a Claude (harness-delegado).

Verifica que, fuera de healthcare, los tools del cuerpo local (dialectic/structure/critique)
DELEGAN a subagentes Sonnet en vez de correr modelos locales débiles o devolver dead-ends;
y que en healthcare se mantienen locales (PHI-safe). Tests rápidos, sin Ollama/MLX.
"""

from __future__ import annotations

import sys
from pathlib import Path
from collections.abc import Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "integrations") not in sys.path:
    sys.path.insert(0, str(ROOT / "integrations"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import mcp_server as m  # noqa: E402


def _fn(tool: object) -> Callable[..., str]:
    return getattr(tool, "fn", tool)  # type: ignore[return-value]  # getattr with object default; callers ensure tool has a callable .fn


# ── _is_healthcare ───────────────────────────────────────────────────────────────
def test_is_healthcare_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    assert m._is_healthcare() is True
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "0")
    assert m._is_healthcare() is False
    monkeypatch.delenv("ARIS4U_HEALTHCARE", raising=False)
    assert m._is_healthcare() is False


# ── dialectic ────────────────────────────────────────────────────────────────────
def test_dialectic_non_phi_delegates_to_sonnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "0")
    out = _fn(m.aris_dialectic)("revisar el login JWT", "auth.py")
    assert 'Agent(model="sonnet")' in out
    assert "BUILDER" in out and "REVIEWER" in out and "SECURITY" in out
    assert "auth.py" in out
    # NO debe haber intentado el path local (sin secciones de output local).
    assert "=== BUILDER ===" not in out


def test_dialectic_healthcare_stays_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    monkeypatch.setenv("ARIS4U_OLLAMA_MAC_URL", "http://localhost:59999")  # puerto muerto
    out = _fn(m.aris_dialectic)("revisar", "x.py")
    # Path local con Ollama caído → mensaje de indisponibilidad, NO la directiva de delegación.
    assert 'Agent(model="sonnet")' not in out
    assert "no disponible" in out.lower() or "no responde" in out.lower()


# ── structure ────────────────────────────────────────────────────────────────────
def test_structure_non_phi_delegates_when_mlx_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "0")
    # Fuerza MLX "frío": route_local devuelve ok=False.
    monkeypatch.setattr(
        m.model_router, "route_local", lambda *a, **k: type("R", (), {"ok": False, "text": None})()
    )
    out = _fn(m.aris_structure)("quiero un sistema de notificaciones multicanal")
    assert 'Agent(model="sonnet"' in out
    assert "STRUCTURE" in out


def test_structure_healthcare_no_delegation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    monkeypatch.setattr(
        m.model_router, "route_local", lambda *a, **k: type("R", (), {"ok": False, "text": None})()
    )
    out = _fn(m.aris_structure)("una idea cruda cualquiera sin estructurar aun")
    assert 'Agent(model="sonnet"' not in out  # healthcare → NO delega


# ── critique ─────────────────────────────────────────────────────────────────────
def test_critique_non_phi_delegates_when_mlx_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "0")
    monkeypatch.setattr(
        m.model_router, "route_local", lambda *a, **k: type("R", (), {"ok": False, "text": None})()
    )
    out = _fn(m.aris_critique)("def add(a, b): return a - b")
    assert 'Agent(model="sonnet"' in out
    assert "CRITIQUE" in out
