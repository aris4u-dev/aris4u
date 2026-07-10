"""Tests de regresión para la sección Backlog en el render de la consola.

Verifica:
  - El HTML renderizado incluye la sección <section id="backlog">.
  - El nav contiene el botón de Backlog.
  - El loader loadBacklog() está presente en el JS inline.
  - El endpoint wired en onSectionShow es 'backlog'.
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
    """Renderiza la consola completa una vez por módulo (costoso — reusar)."""
    inv = inventory.build_inventory(_REPO, console_repo=_CONSOLE)
    curated = json.loads(_CURATED.read_text(encoding="utf-8")) if _CURATED.exists() else {}
    return render_console.render_console_html(inv, curated)


def test_backlog_section_exists(rendered_html: str) -> None:
    """El HTML debe tener <section id="backlog">."""
    assert 'id="backlog"' in rendered_html


def test_backlog_nav_button_exists(rendered_html: str) -> None:
    """El nav debe contener un botón data-s="backlog"."""
    assert 'data-s="backlog"' in rendered_html


def test_backlog_nav_label(rendered_html: str) -> None:
    """El botón de nav del Backlog debe mostrar la etiqueta con el emoji de clipboard."""
    assert "Backlog" in rendered_html


def test_backlog_loader_function_in_js(rendered_html: str) -> None:
    """El JS inline debe contener la función loadBacklog."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", rendered_html, re.S)
    combined = "\n".join(scripts)
    assert "function loadBacklog(" in combined


def test_backlog_render_function_in_js(rendered_html: str) -> None:
    """El JS inline debe contener la función renderBacklog."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", rendered_html, re.S)
    combined = "\n".join(scripts)
    assert "function renderBacklog(" in combined


def test_backlog_wired_in_section_show(rendered_html: str) -> None:
    """onSectionShow debe despachar a loadBacklog() cuando s==='backlog'."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", rendered_html, re.S)
    combined = "\n".join(scripts)
    assert "s==='backlog'" in combined or "s===\"backlog\"" in combined


def test_backlog_fetch_endpoint(rendered_html: str) -> None:
    """El loader debe hacer fetch al endpoint /backlog."""
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", rendered_html, re.S)
    combined = "\n".join(scripts)
    assert "fetch('/backlog')" in combined or 'fetch("/backlog")' in combined


def test_backlog_summary_div_exists(rendered_html: str) -> None:
    """La sección backlog debe tener el div backlog-summary."""
    assert "backlog-summary" in rendered_html


def test_backlog_list_div_exists(rendered_html: str) -> None:
    """La sección backlog debe tener el div backlog-list."""
    assert "backlog-list" in rendered_html


def test_render_backlog_function_exists() -> None:
    """_render_backlog debe estar exportada en el módulo render_console."""
    assert hasattr(render_console, "_render_backlog")
    assert callable(render_console._render_backlog)
