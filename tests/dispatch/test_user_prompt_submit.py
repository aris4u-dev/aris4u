"""Tests UserPromptSubmit: handler python (dispatch) — estructura de la respuesta.

El RECALL híbrido (FTS5 + semántico) lee la DB VIVA, así que su CONTENIDO varía entre
corridas. Por eso comparamos la ESTRUCTURA (qué secciones aparecen), no bytes exactos:
MODEL_HINT, EFFORT, DEPTH, y — en prompts no-simple — el bloque 🧠 RECALL.

Nota: los tests de equivalencia new-vs-depth_inject.sh se eliminaron porque depth_inject.sh
fue portado al dispatcher Python y borrado. La cobertura de estructura vive en
test_shadow_mode_omits_depth y test_sections_ignores_recall_embedded_markers.

Corre:  .venv312/bin/python3 -m pytest tests/dispatch/test_user_prompt_submit.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
INVOKE = str(ROOT / "tests" / "dispatch" / "_invoke.py")
FIXDIR = Path(__file__).resolve().parent / "fixtures"


def _run_new(fixture: str, depth_protocol: str = "1") -> str:
    """Corre el handler nuevo vía _invoke (lee JSON del evento) → additionalContext.

    `depth_protocol` fija ARIS4U_DEPTH_PROTOCOL en el subprocess para no depender del
    ambiente: '1' (default) testea el modo on; '0' el modo sombra de WS-A.
    """
    payload = (FIXDIR / fixture).read_text()
    env = dict(os.environ)
    env["ARIS4U_DEPTH_PROTOCOL"] = depth_protocol
    proc = subprocess.run(
        [PY, INVOKE, "user_prompt_submit", "UserPromptSubmit"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    out = proc.stdout.strip()
    if not out:
        return ""
    return json.loads(out).get("additionalContext", "")


def _sections(text: str) -> dict:
    """Marca presencia de cada sección estructural (independiente del contenido vivo).

    Los marcadores de cognición (DEPTH/EFFORT/MODEL_HINT) se inyectan SIEMPRE al inicio
    de su línea. El bloque 🧠 RECALL mete contenido VIVO de memoria que puede contener
    esos literales a media línea (p.ej. un digest que menciona "DEPTH:"). Por eso el
    match se ancla al inicio de línea: así el RECALL no produce falsos positivos (era la
    causa de la flakiness de test_shadow_mode_omits_depth).
    """
    lines = [ln.lstrip() for ln in text.splitlines()]

    def _starts(prefix: str) -> bool:
        return any(ln.startswith(prefix) for ln in lines)

    return {
        "model_hint": _starts("🧭 MODEL_HINT:"),
        "effort": _starts("EFFORT:"),
        "depth": _starts("DEPTH:"),
        "depth_simple": _starts("DEPTH: simple"),
        "recall": "🧠 RECALL" in text,
        "fable": "Fable 5" in text,
        "sonnet_haiku": "Sonnet 4.6 / Haiku 4.5" in text,
    }


def test_shadow_mode_omits_depth() -> None:
    """WS-A: con ARIS4U_DEPTH_PROTOCOL=0 NO se inyecta cognición (DEPTH/EFFORT/MODEL_HINT).

    El foso (RECALL/decisiones) puede o no aparecer según haya memoria relevante, pero la
    profundidad/effort/model_hint nunca deben inyectarse en modo sombra.
    """
    shadow = _run_new("user_prompt_submit_strategy.json", depth_protocol="0")
    s = _sections(shadow)
    assert not s["depth"], f"shadow no debe inyectar DEPTH: {shadow!r}"
    assert not s["effort"]
    assert not s["model_hint"]


def test_sections_ignores_recall_embedded_markers() -> None:
    """Regresión (flakiness de shadow): _sections NO debe confundir literales
    DEPTH:/EFFORT:/MODEL_HINT: que vengan DENTRO del bloque 🧠 RECALL (contenido vivo de
    memoria) con la inyección estructural de cognición. Match anclado a inicio de línea."""
    shadow_like = (
        "🧠 RECALL (memoria ARIS4U relevante):\n"
        "  ~0.61 [digest#42] La sesión cubrió DEPTH: profundo y EFFORT: alto en el plan.\n"
        "  ~0.55 [decision#7] Se decidió que 🧭 MODEL_HINT: Fable era advisory.\n"
    )
    s = _sections(shadow_like)
    assert not s["depth"], "DEPTH: dentro del RECALL no es inyección estructural"
    assert not s["effort"]
    assert not s["model_hint"]
    assert s["recall"]
    # Un marcador estructural REAL (inicio de línea) sí se detecta:
    real = "DEPTH: decision | L1,L2\nEFFORT: HIGH\n🧭 MODEL_HINT: Fable 5 (advisory)"
    s2 = _sections(real)
    assert s2["depth"] and s2["effort"] and s2["model_hint"]


def test_short_prompt_is_noop() -> None:
    # <5 chars → passthrough (sin additionalContext), igual que el early-exit del .sh.
    payload = json.dumps({"prompt": "hi", "cwd": str(ROOT)})
    proc = subprocess.run(
        [PY, INVOKE, "user_prompt_submit", "UserPromptSubmit"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.stdout.strip() == ""


def test_recall_respects_2s_cap() -> None:
    # El cap SIGALRM(2s) + overhead del engine deben mantener el hook holgado < 8s.
    t0 = time.perf_counter()
    _run_new("user_prompt_submit_strategy.json")
    elapsed = time.perf_counter() - t0
    assert elapsed < 8.0, f"hook tardó {elapsed:.2f}s (cap de recall = 2s)"


if __name__ == "__main__":
    sys.exit(subprocess.call([PY, "-m", "pytest", __file__, "-v"]))
