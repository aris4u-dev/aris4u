"""Gate de Fase A para la capa multi-modelo (engine/v16/model_router).

Verifica el COMPORTAMIENTO que el plan exige, no la implementación:
  - política → selecciona el primer candidato vivo;
  - health-awareness: salta modelos no instalados (absorbe el drift config↔real);
  - fallback en cascada Mac→W2 cuando el primario no responde;
  - fail-open: nada respondió → ok=False, text=None, sin excepción;
  - invariante de privacidad: route_local jamás toca un backend no-local;
  - telemetría: cada ruteo emite un evento model_route.

Sin red ni Ollama reales: se parchea `_live_models` (health) y `_dispatch`
(transporte). Patrón de mock idéntico al resto de la suite (subprocess/MagicMock).
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.v16 import model_router as mr  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    """Cada test arranca con la cache de health limpia."""
    mr._model_cache.clear()
    yield
    mr._model_cache.clear()


def test_primary_alive_wins():
    """Si el primer candidato (mlx/cuerpo 27B) está vivo y responde, gana."""
    with patch.object(mr, "_live_models", return_value={mr.MLX_MODEL}), \
         patch.object(mr, "_dispatch", return_value="ok-text") as disp:
        res = mr.route_local("dialectic", "hola")
    assert res.ok is True
    assert res.text == "ok-text"
    assert res.backend == "mlx"
    assert res.model == mr.MLX_MODEL
    assert res.fallback_used is False
    assert disp.call_count == 1


def test_mlx_skipped_when_server_down():
    """Fase 3b: si el mlx_lm.server no corre, el 27B se salta (health-aware) y cae a
    Foundation-Sec Mac — el cuerpo local es lazy/fail-open, no una dependencia dura."""
    def fake_live(backend):
        return {mr._FSEC} if backend == "mac" else set()  # mlx y w2 caídos
    with patch.object(mr, "_live_models", side_effect=fake_live), \
         patch.object(mr, "_dispatch", return_value="from-fsec") as disp:
        res = mr.route_local("dialectic", "x")
    assert res.ok is True
    assert res.backend == "mac"
    assert res.model == mr._FSEC
    assert res.fallback_used is True  # el candidato mlx (idx 0) se saltó
    assert disp.call_count == 1


def test_skips_uninstalled_model_drift():
    """El bug real: el primario Mac NO está instalado → se salta al siguiente vivo (W2).

    Simula el Mac sin Foundation-Sec (drift de config) y solo qwen3:8b vivo en W2.
    """
    def fake_dispatch(cand, prompt, system, timeout, score_out=None):
        return "from-w2" if cand.model == "qwen3:8b" else None

    with patch.object(mr, "_live_models", return_value={"qwen3:8b"}), \
         patch.object(mr, "_dispatch", side_effect=fake_dispatch):
        res = mr.route_local("dialectic", "x")
    assert res.ok is True
    assert res.model == "qwen3:8b"
    assert res.fallback_used is True  # no fue el primer candidato (mac)


def test_cascade_to_w2_when_mac_down():
    """Mac caído (sin modelos vivos) → cae al candidato W2."""
    def fake_live(backend):
        return {"qwen3:8b"} if backend == "w2" else set()

    with patch.object(mr, "_live_models", side_effect=fake_live), \
         patch.object(mr, "_dispatch", return_value="from-w2") as disp:
        res = mr.route_local("dialectic", "x")
    assert res.ok is True
    assert res.backend == "w2"
    assert res.model == "qwen3:8b"
    # Solo se intentó el candidato W2 (los Mac se saltaron por health).
    assert disp.call_count == 1


def test_fail_open_when_nothing_alive():
    """Nada vivo en ningún backend → ok=False, text=None, sin excepción."""
    with patch.object(mr, "_live_models", return_value=set()), \
         patch.object(mr, "_dispatch", return_value=None) as disp:
        res = mr.route_local("dialectic", "x")
    assert res.ok is False
    assert res.text is None
    assert res.candidates_tried == 0
    assert disp.call_count == 0  # ningún modelo vivo: ni se intentó


def test_fail_open_when_all_alive_but_silent():
    """Modelos vivos pero todos devuelven None → fail-open, se intentaron todos."""
    alive = {mr._FSEC, "qwen3:8b"}
    with patch.object(mr, "_live_models", return_value=alive), \
         patch.object(mr, "_dispatch", return_value=None) as disp:
        res = mr.route_local("dialectic", "x")
    assert res.ok is False
    assert res.candidates_tried == 2  # dialectic = [mac Foundation-Sec, w2 qwen3:8b]
    assert disp.call_count == 2


def test_privacy_invariant_only_local_backends():
    """route_local nunca debe intentar un backend fuera de _LOCAL_BACKENDS.

    Se inyecta un candidato 'gemini' (online) en la política y se confirma que
    jamás se despacha, aunque su modelo figure 'vivo'.
    """
    poisoned = [
        mr.Candidate("gemini", "gemini-2.5-flash", {}),
        mr.Candidate("mac", "qwen35-analyst:latest", {}),
    ]
    seen_backends = []

    def fake_dispatch(cand, prompt, system, timeout, score_out=None):
        seen_backends.append(cand.backend)
        return "ok"

    with patch.dict(mr._POLICY, {"_poison": poisoned}), \
         patch.object(mr, "_live_models", return_value={"gemini-2.5-flash", "qwen35-analyst:latest"}), \
         patch.object(mr, "_dispatch", side_effect=fake_dispatch):
        res = mr.route_local("_poison", "x")
    assert "gemini" not in seen_backends
    assert res.backend == "mac"


def test_unknown_task_falls_back_to_default_policy():
    """Una task sin política usa 'default' en vez de romper."""
    with patch.object(mr, "_live_models", return_value={mr._FSEC}), \
         patch.object(mr, "_dispatch", return_value="ok"):
        res = mr.route_local("task-que-no-existe", "x")
    assert res.ok is True
    assert res.model == mr._FSEC  # primer candidato de 'default'


def test_telemetry_emitted(tmp_path, monkeypatch):
    """Cada ruteo (éxito o fallo) emite un evento model_route a la telemetría."""
    log = tmp_path / "logs" / "v16.1-events.jsonl"
    log.parent.mkdir(parents=True)
    monkeypatch.setattr(mr, "ARIS4U_ROOT", tmp_path)

    with patch.object(mr, "_live_models", return_value={mr._FSEC}), \
         patch.object(mr, "_dispatch", return_value="ok"):
        mr.route_local("dialectic", "x", client="client-c")

    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1
    import json
    ev = json.loads(lines[0])
    assert ev["event"] == "model_route"
    assert ev["ok"] is True
    assert ev["task"] == "dialectic"
    assert ev["client"] == "client-c"


def test_health_cache_ttl(monkeypatch):
    """_live_models cachea por backend dentro del TTL (no remartilla Ollama)."""
    calls = {"n": 0}

    def fake_query():
        calls["n"] += 1
        return {"qwen35-analyst:latest"}

    monkeypatch.setattr(mr, "_query_mac_models", fake_query)
    a = mr._live_models("mac")
    b = mr._live_models("mac")
    assert a == b == {"qwen35-analyst:latest"}
    assert calls["n"] == 1  # segunda llamada vino de cache


def test_negative_cache_shorter_ttl(monkeypatch):
    """Un resultado VACÍO expira rápido (probable blip); uno NO vacío persiste."""
    import time as _t
    calls = {"n": 0}

    def fake_query():
        calls["n"] += 1
        return set()  # blip: Ollama no respondió

    monkeypatch.setattr(mr, "_query_mac_models", fake_query)
    # Cache vacío 'viejo' (15s > _CACHE_TTL_EMPTY=10) → debe re-consultar.
    mr._model_cache["mac"] = (_t.time() - 15, set())
    mr._live_models("mac")
    assert calls["n"] == 1
    # Cache NO vacío a 15s (< _CACHE_TTL=60) → servido de cache, sin re-consultar.
    mr._model_cache["mac"] = (_t.time() - 15, {"qwen35-analyst:latest"})
    assert mr._live_models("mac") == {"qwen35-analyst:latest"}
    assert calls["n"] == 1


# ── Fase C: gate ONLINE fail-closed (tests de FUGA) ─────────────────────────────

def _local_stub():
    """RouteResult local cualquiera, para detectar la caída a route_local."""
    return mr.RouteResult(text="LOCAL", backend="mac", model="m", ok=True,
                          fallback_used=False, latency_ms=0, candidates_tried=1)


def test_local_policies_have_no_online_backend():
    """Fable #4: ninguna task de _POLICY (la que usa route_local) trae backend online."""
    for task, cands in mr._POLICY.items():
        for c in cands:
            assert c.backend in mr._LOCAL_BACKENDS, f"{task} expone backend {c.backend}"


def test_route_denies_online_with_client():
    """Contexto de cliente → jamás a un tercero (fail-closed)."""
    with patch.object(mr, "dispatch_grok") as grok, \
         patch.object(mr, "route_local", return_value=_local_stub()) as loc:
        mr.route("outside_view", "pregunta inocua", allow_online=True, client="client-c")
    grok.assert_not_called()
    loc.assert_called_once()


def test_route_denies_online_with_secret():
    """Un secreto en el texto → no elegible para online."""
    with patch.object(mr, "dispatch_grok") as grok, \
         patch.object(mr, "route_local", return_value=_local_stub()):
        mr.route("outside_view", "mi token: sk-abcdefghij0123456789xyz", allow_online=True)
    grok.assert_not_called()


def test_route_denies_online_with_phi():
    """Un marcador PHI → no elegible para online."""
    with patch.object(mr, "dispatch_grok") as grok, \
         patch.object(mr, "route_local", return_value=_local_stub()):
        mr.route("outside_view", "el paciente tiene historia clínica reciente", allow_online=True)
    grok.assert_not_called()


def test_route_denies_online_when_not_allowed():
    """allow_online=False (default) → jamás online."""
    with patch.object(mr, "dispatch_grok") as grok, \
         patch.object(mr, "route_local", return_value=_local_stub()):
        mr.route("outside_view", "pregunta inocua")
    grok.assert_not_called()


def test_route_denies_online_when_sensitive_flag():
    """sensitive=True del caller → jamás online aunque el texto parezca limpio."""
    with patch.object(mr, "dispatch_grok") as grok, \
         patch.object(mr, "route_local", return_value=_local_stub()):
        mr.route("outside_view", "pregunta inocua", allow_online=True, sensitive=True)
    grok.assert_not_called()


def test_route_allows_online_when_clean():
    """Contenido limpio + opt-in + sin cliente → SÍ va a online; no cae a local."""
    with patch.object(mr, "dispatch_grok", return_value="respuesta de grok") as grok, \
         patch.object(mr, "route_local") as loc:
        res = mr.route("outside_view", "¿cuál es la capital de Francia?", allow_online=True)
    grok.assert_called_once()
    loc.assert_not_called()
    assert res.ok and res.backend == "grok" and res.text == "respuesta de grok"


def test_route_falls_back_local_when_online_fails():
    """Elegible pero el online no respondió → fail-open a route_local."""
    with patch.object(mr, "dispatch_grok", return_value=None) as grok, \
         patch.object(mr, "route_local", return_value=_local_stub()) as loc:
        res = mr.route("outside_view", "pregunta inocua", allow_online=True)
    grok.assert_called_once()
    loc.assert_called_once()
    assert res.text == "LOCAL"
