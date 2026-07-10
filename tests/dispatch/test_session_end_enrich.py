"""Gate de Fase B — enriquecimiento local del digest (session_end._enrich_summary_local).

Verifica el contrato: el summary factual SE PRESERVA y la narrativa solo se añade
cuando el router local respondió; fail-open intacto si no; desactivable por env.
Sin Ollama real: se parchea model_router.route_local.
"""
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"
sys.path.insert(0, str(HOOKS))
sys.path.insert(0, str(ROOT))

from dispatch.events.session_end import _enrich_summary_local  # noqa: E402


class _R:
    def __init__(self, ok, text):
        self.ok = ok
        self.text = text


def test_appends_narrative_when_router_ok():
    from engine.v16 import model_router
    with patch.object(model_router, "route_local",
                      return_value=_R(True, "Construido el router multi-modelo y cerrados 3 riesgos.")):
        out = _enrich_summary_local("DB: 5 decisions.", 4, 120, "router", "")
    assert out.startswith("DB: 5 decisions.")      # factual preservado
    assert "router multi-modelo" in out            # narrativa añadida


def test_fail_open_when_router_not_ok():
    from engine.v16 import model_router
    with patch.object(model_router, "route_local", return_value=_R(False, None)):
        out = _enrich_summary_local("DB: 5 decisions.", 0, 0, "", "")
    assert out == "DB: 5 decisions."               # summary intacto


def test_strips_bullet_and_caps_first_line():
    from engine.v16 import model_router
    raw = "  * La sesión cerró 3 riesgos del router.\nlínea extra ignorada"
    with patch.object(model_router, "route_local", return_value=_R(True, raw)):
        out = _enrich_summary_local("S.", 1, 1, "x", "y")
    assert out == "S. La sesión cerró 3 riesgos del router."  # 1ª línea, sin viñeta


def test_disabled_by_env(monkeypatch):
    monkeypatch.setenv("ARIS4U_DIGEST_NARRATIVE", "0")
    from engine.v16 import model_router
    with patch.object(model_router, "route_local") as rl:
        out = _enrich_summary_local("S.", 1, 1, "x", "y")
    assert out == "S."
    rl.assert_not_called()                          # ni se intenta el router
