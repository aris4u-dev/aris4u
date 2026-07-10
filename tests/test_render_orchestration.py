"""Tests for architecture/render_orchestration.py.

Cobertura del renderizador del mapa de orquestación (orchestration_map.json → HTML).
No hay código fuente que modificar: estos tests cargan el módulo por ruta de archivo
(architecture/ no es un paquete importable) y monkeypatchean los constantes a nivel de
módulo ``DATA`` (entrada JSON) y ``OUT`` (salida HTML) hacia ``tmp_path``.

REGLA SAGRADA: el ``OUT`` por defecto del módulo apunta a ``~/Desktop`` — TODOS los
tests redirigen ``OUT`` a ``tmp_path`` para NUNCA escribir en el escritorio real, y
``DATA`` a un JSON de prueba para no depender del orchestration_map.json vivo.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = _REPO_ROOT / "tools" / "render_orchestration.py"


def _load_module() -> ModuleType:
    """Carga render_orchestration.py por ruta como módulo aislado.

    El paquete ``architecture/`` no tiene ``__init__.py``, así que no es importable
    por nombre. Cargar por spec evita tocar el árbol de paquetes y deja un módulo
    fresco cuyos constantes (``DATA``/``OUT``) podemos monkeypatchear sin contaminar
    otras pruebas.

    Returns:
        El módulo render_orchestration recién ejecutado.
    """
    spec = importlib.util.spec_from_file_location(
        "render_orchestration_under_test", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # render_orchestration.py lee sys.argv[1] en tiempo de import (default OUT).
    # Garantizamos un argv inofensivo para que el import no explote ni capture
    # argumentos de pytest.
    saved_argv = sys.argv
    sys.argv = ["render_orchestration.py"]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = saved_argv
    return module


@pytest.fixture
def render_mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Módulo cargado con ``OUT`` redirigido a tmp_path (NUNCA al Desktop real).

    ``DATA`` se deja como está; cada test lo apunta a su propio JSON vía
    ``_write_data`` o monkeypatch. ``OUT`` siempre cae en tmp_path.
    """
    mod = _load_module()
    out_path = tmp_path / "out.html"
    monkeypatch.setattr(mod, "OUT", out_path)
    return mod


def _minimal_map() -> dict[str, Any]:
    """Construye un orchestration_map mínimo pero completo.

    Cubre: meta con todos los campos que render() consume, dos eventos, pasos
    con cada ejecutor/nativez relevante (incluido un hallazgo no-nativo,
    un paso bloqueante, un paso paralelo, un parallel_block y notes).
    """
    return {
        "_meta": {
            "title": "Mapa <de> Prueba & Cía",  # incluye chars que deben escaparse
            "generated": "2026-06-19",
            "method": "test fixture",
            "concurrency_reality": "secuencial dentro de cada handler",
            "lifecycle": "SessionStart → ... → SessionEnd",
        },
        "events": [
            {
                "event": "SessionStart",
                "dep_claude": "antes",
                "blocking": False,
                "trigger": "al abrir sesión",
                "summary": "arranque del dispatcher",
                "steps": [
                    {
                        "n": 1,
                        "name": "dispatch entrypoint",
                        "file_line": "hooks/dispatch.py:26-36",
                        "executor": "mecanico_local",
                        "concurrency": "secuencial",
                        "nativez": "nativo",
                    },
                    {
                        "n": 2,
                        "name": "clasificación local débil",
                        "file_line": "engine/f1.py:10",
                        "executor": "modelo_local",
                        "concurrency": "paralelo",
                        "nativez": "mejor_en_claude",
                        "why": "un modelo de 8B alucina; Claude lo haría mejor",
                        "blocking": True,
                    },
                ],
                "parallel_blocks": [
                    {"desc": "dispatcher || validator OS-level (async:true)"}
                ],
                "notes": "fail-open en todo el handler",
            },
            {
                "event": "PreToolUse",
                "dep_claude": "bloquea",
                "blocking": True,
                "trigger": "antes de cada tool",
                "summary": "guards advisory/block",
                "steps": [
                    {
                        "n": 1,
                        "name": "migration_linter guard",
                        "file_line": "hooks/dispatch/events/pre.py:5",
                        "executor": "intercepta",
                        "concurrency": "secuencial",
                        "nativez": "pegado",
                    },
                    {
                        "n": 2,
                        "name": "inyección de RECALL",
                        "file_line": "hooks/dispatch/events/pre.py:40",
                        "executor": "augmenta",
                        "concurrency": "secuencial",
                        "nativez": "redundante",
                    },
                    {
                        "n": 3,
                        "name": "Claude piensa",
                        "file_line": "n/a",
                        "executor": "claude",
                        "concurrency": "secuencial",
                        "nativez": "nativo",
                    },
                ],
            },
        ],
    }


def _write_data(mod: ModuleType, data: Any, tmp_path: Path,
                monkeypatch: pytest.MonkeyPatch) -> Path:
    """Escribe ``data`` (dict→JSON, o str crudo) y apunta mod.DATA ahí.

    Returns:
        La ruta del JSON de entrada en tmp_path.
    """
    data_path = tmp_path / "orchestration_map.json"
    if isinstance(data, str):
        data_path.write_text(data)
    else:
        data_path.write_text(json.dumps(data, ensure_ascii=False))
    monkeypatch.setattr(mod, "DATA", data_path)
    return data_path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_render_produces_valid_html_file(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """render() escribe un HTML bien formado a OUT y NO toca el Desktop real."""
    _write_data(render_mod, _minimal_map(), tmp_path, monkeypatch)

    render_mod.render()

    out = render_mod.OUT
    assert out.exists(), "render() debe escribir el archivo de salida"
    # Confirma que OUT vive en tmp_path (jamás en ~/Desktop).
    assert str(tmp_path) in str(out)
    html = out.read_text()
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "<head>" in html and "</body></html>" in html


def test_render_includes_event_nodes(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El HTML contiene el nombre de cada evento del mapa."""
    _write_data(render_mod, _minimal_map(), tmp_path, monkeypatch)

    render_mod.render()
    html = render_mod.OUT.read_text()

    assert "SessionStart" in html
    assert "PreToolUse" in html
    # Nombres de pasos presentes
    assert "dispatch entrypoint" in html
    assert "migration_linter guard" in html


def test_render_includes_step_file_lines_and_executors(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Los file_line y las etiquetas de ejecutor aparecen renderizados."""
    _write_data(render_mod, _minimal_map(), tmp_path, monkeypatch)

    render_mod.render()
    html = render_mod.OUT.read_text()

    assert "hooks/dispatch.py:26-36" in html
    # Etiquetas legibles de EXEC_COLOR
    assert "MECÁNICO" in html
    assert "MODELO LOCAL" in html
    assert "CLAUDE" in html


def test_render_escapes_html_in_meta_title(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Caracteres especiales del título se escapan (no se inyecta HTML crudo)."""
    _write_data(render_mod, _minimal_map(), tmp_path, monkeypatch)

    render_mod.render()
    html = render_mod.OUT.read_text()

    # "Mapa <de> Prueba & Cía" → debe aparecer escapado, nunca el '<de>' crudo.
    assert "Mapa &lt;de&gt; Prueba &amp; Cía" in html
    assert "Mapa <de> Prueba" not in html


def test_render_findings_section_lists_non_native(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Los hallazgos no-nativos (mejor_en_claude/redundante) salen en la sección."""
    _write_data(render_mod, _minimal_map(), tmp_path, monkeypatch)

    render_mod.render()
    html = render_mod.OUT.read_text()

    assert "Hallazgos" in html
    # 'why' del paso mejor_en_claude
    assert "un modelo de 8B alucina" in html


def test_render_counts_steps_in_kpis(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El KPI de pasos totales refleja la suma real de pasos del mapa."""
    data = _minimal_map()
    total = sum(len(e["steps"]) for e in data["events"])  # 2 + 3 = 5
    _write_data(render_mod, data, tmp_path, monkeypatch)

    render_mod.render()
    html = render_mod.OUT.read_text()

    # Render emite "<b>{total_steps}</b><span>pasos mapeados</span>"
    assert f"<b>{total}</b><span>pasos mapeados</span>" in html


def test_render_prints_ok_summary(
    render_mod: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """render() imprime un resumen 'OK → ...' a stdout al terminar."""
    _write_data(render_mod, _minimal_map(), tmp_path, monkeypatch)

    render_mod.render()

    captured = capsys.readouterr()
    assert "OK →" in captured.out
    assert "pasos" in captured.out


def test_render_step_defaults_when_optional_fields_missing(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pasos sin executor/concurrency/nativez usan los defaults sin crashear.

    El código hace s.get('executor','mecanico_local'), etc. Un paso pelado
    (solo n/name/file_line) debe rendear como MECÁNICO/secuencial/nativo.
    """
    data = {
        "_meta": {
            "title": "Defaults",
            "generated": "2026-06-19",
            "method": "m",
            "concurrency_reality": "c",
            "lifecycle": "l",
        },
        "events": [
            {
                "event": "Stop",
                "steps": [
                    {"n": 1, "name": "paso pelado", "file_line": "x.py:1"},
                ],
            }
        ],
    }
    _write_data(render_mod, data, tmp_path, monkeypatch)

    render_mod.render()  # no debe lanzar
    html = render_mod.OUT.read_text()
    assert "paso pelado" in html
    assert "MECÁNICO" in html  # default executor → label


# ---------------------------------------------------------------------------
# Edge cases / fail behavior
# ---------------------------------------------------------------------------


def test_render_missing_json_raises_filenotfound(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el JSON de entrada no existe, render() falla limpio (no escribe basura).

    El módulo no maneja el FileNotFoundError internamente; verificamos que el
    fallo es explícito y que NO se creó un OUT a medias.
    """
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(render_mod, "DATA", missing)

    with pytest.raises(FileNotFoundError):
        render_mod.render()

    assert not render_mod.OUT.exists()


def test_render_malformed_json_raises_jsondecodeerror(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON corrupto produce JSONDecodeError explícito, sin escribir OUT."""
    _write_data(render_mod, "{ esto no es json valido ", tmp_path, monkeypatch)

    with pytest.raises(json.JSONDecodeError):
        render_mod.render()

    assert not render_mod.OUT.exists()


def test_render_missing_meta_key_raises_keyerror(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Falta '_meta' → KeyError claro (estructura inválida detectada)."""
    _write_data(render_mod, {"events": []}, tmp_path, monkeypatch)

    with pytest.raises(KeyError):
        render_mod.render()


def test_render_empty_events_still_writes_html(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con events=[] (válido) render() produce HTML con 0 pasos, sin crashear."""
    data = {
        "_meta": {
            "title": "Vacío",
            "generated": "2026-06-19",
            "method": "m",
            "concurrency_reality": "c",
            "lifecycle": "l",
        },
        "events": [],
    }
    _write_data(render_mod, data, tmp_path, monkeypatch)

    render_mod.render()
    html = render_mod.OUT.read_text()

    assert "<b>0</b><span>pasos mapeados</span>" in html
    assert html.rstrip().endswith("</html>")


def test_render_unknown_executor_raises_keyerror(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un executor fuera de EXEC_COLOR revienta en EXEC_COLOR[ex] (KeyError).

    Documenta el comportamiento real: el render NO valida el vocabulario de
    executor en la tabla por-evento (línea ``c,emo,lbl = EXEC_COLOR[ex]``).
    """
    data = {
        "_meta": {
            "title": "Bad executor",
            "generated": "2026-06-19",
            "method": "m",
            "concurrency_reality": "c",
            "lifecycle": "l",
        },
        "events": [
            {
                "event": "PostToolUse",
                "steps": [
                    {
                        "n": 1,
                        "name": "paso raro",
                        "file_line": "x.py:1",
                        "executor": "ejecutor_inexistente",
                    }
                ],
            }
        ],
    }
    _write_data(render_mod, data, tmp_path, monkeypatch)

    with pytest.raises(KeyError):
        render_mod.render()


def test_render_against_live_orchestration_map(
    render_mod: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Smoke contra el orchestration_map.json REAL del repo (solo lectura).

    Garantiza que el renderizador procesa el mapa vivo de extremo a extremo.
    OUT sigue redirigido a tmp_path; el JSON real solo se lee. Se omite si el
    archivo no está presente (entornos de checkout parcial).
    """
    live = _REPO_ROOT / "architecture" / "orchestration_map.json"
    if not live.exists():
        pytest.skip("orchestration_map.json vivo no presente")

    monkeypatch.setattr(render_mod, "DATA", live)
    render_mod.render()

    html = render_mod.OUT.read_text()
    assert html.startswith("<!doctype html>")
    # Los 7 eventos canónicos del lifecycle deben aparecer.
    for event in (
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "SubagentStart",
        "Stop",
        "SessionEnd",
    ):
        assert event in html
