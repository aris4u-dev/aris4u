"""Tests de regresión para la sección Plantillas en el render de la consola.

Verifica:
  - El HTML renderizado incluye la sección <section id="plantillas">.
  - El nav contiene el botón data-s="plantillas".
  - El loader loadSkeletons() está presente en el JS inline.
  - El render de skeletons está cableado en onSectionShow.
  - El fetch al endpoint /skeletons está en el JS.
  - Los divs skel-summary y skel-list existen en la sección.
  - La función _render_plantillas es callable desde el módulo.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from aris4u_console import inventory, render_console

_CONSOLE = Path(__file__).resolve().parent.parent
_REPO = _CONSOLE.parent
_CURATED = _CONSOLE / "aris4u_console" / "data" / "curated_semantics.json"


@pytest.fixture(scope="module")
def rendered_html() -> str:
    """Renderiza la consola completa una vez por módulo."""
    inv = inventory.build_inventory(_REPO, console_repo=_CONSOLE)
    curated = json.loads(_CURATED.read_text(encoding="utf-8")) if _CURATED.exists() else {}
    return render_console.render_console_html(inv, curated)


def test_plantillas_section_exists(rendered_html: str) -> None:
    """El HTML debe tener <section id="plantillas">."""
    assert 'id="plantillas"' in rendered_html


def test_plantillas_nav_button_exists(rendered_html: str) -> None:
    """El nav debe contener un botón data-s="plantillas"."""
    assert 'data-s="plantillas"' in rendered_html


def test_plantillas_nav_label(rendered_html: str) -> None:
    """El botón de nav debe mostrar la etiqueta Plantillas."""
    assert "Plantillas" in rendered_html


def test_plantillas_loader_function_in_js(rendered_html: str) -> None:
    """El JS inline debe contener la función loadSkeletons."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", rendered_html, re.S)
    combined = "\n".join(scripts)
    assert "function loadSkeletons(" in combined


def test_plantillas_render_function_in_js(rendered_html: str) -> None:
    """El JS inline debe contener la función _renderSkeletons."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", rendered_html, re.S)
    combined = "\n".join(scripts)
    assert "function _renderSkeletons(" in combined


def test_plantillas_wired_in_section_show(rendered_html: str) -> None:
    """onSectionShow debe despachar a loadSkeletons() cuando s==='plantillas'."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", rendered_html, re.S)
    combined = "\n".join(scripts)
    assert "s==='plantillas'" in combined or 's==="plantillas"' in combined


def test_plantillas_fetch_endpoint(rendered_html: str) -> None:
    """El loader debe hacer fetch al endpoint /skeletons."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", rendered_html, re.S)
    combined = "\n".join(scripts)
    assert "fetch('/skeletons')" in combined or 'fetch("/skeletons")' in combined


def test_plantillas_summary_div_exists(rendered_html: str) -> None:
    """La sección plantillas debe tener el div skel-summary."""
    assert "skel-summary" in rendered_html


def test_plantillas_list_div_exists(rendered_html: str) -> None:
    """La sección plantillas debe tener el div skel-list."""
    assert "skel-list" in rendered_html


def test_render_plantillas_function_exists() -> None:
    """_render_plantillas debe estar exportada en el módulo render_console."""
    assert hasattr(render_console, "_render_plantillas")
    assert callable(render_console._render_plantillas)
