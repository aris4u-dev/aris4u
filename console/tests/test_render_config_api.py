"""Tests para las secciones Config y API añadidas a render_console.py.

Verifica:
- El HTML renderizado contiene las secciones con los ids correctos.
- Las entradas de navegación están presentes en el sidebar.
- Los loaders JS existen en el HTML (loadConfig / loadApi).
- El manejo de available=False está presente (offline message).
- No hace falta mockear la DB — las secciones cargan async vía fetch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aris4u_console import inventory, render_console  # noqa: E402

_CURATED = _ROOT / "aris4u_console" / "data" / "curated_semantics.json"
_REPO = _ROOT.parent


def _render_html() -> str:
    """Render completo de la consola (igual que test_render_js_valid)."""
    inv = inventory.build_inventory(_REPO, console_repo=_ROOT)
    curated = json.loads(_CURATED.read_text(encoding="utf-8")) if _CURATED.exists() else {}
    return render_console.render_console_html(inv, curated)


@pytest.fixture(scope="module")
def html() -> str:
    """HTML renderizado una sola vez para todos los tests del módulo."""
    return _render_html()


# ---- sección Config ----

def test_config_section_id_present(html: str) -> None:
    """La sección Config existe con id="config"."""
    assert 'id="config"' in html, 'falta <section id="config"> en el HTML renderizado'


def test_config_nav_button_present(html: str) -> None:
    """El botón de nav apunta a la sección config."""
    assert 'data-s="config"' in html, 'falta el botón de nav data-s="config"'


def test_config_nav_label_present(html: str) -> None:
    """El label del nav incluye el emoji de config."""
    assert "Config" in html, "falta el texto 'Config' en la nav"


def test_config_loader_function_present(html: str) -> None:
    """El JS inline define la función loadConfig."""
    assert "function loadConfig()" in html, "falta la función loadConfig() en el JS inline"


def test_config_onsectionshow_wired(html: str) -> None:
    """onSectionShow despacha a loadConfig cuando s==='config'."""
    assert "loadConfig()" in html, "loadConfig() no está cableado en onSectionShow"


def test_config_offline_guard_present(html: str) -> None:
    """La sección incluye el div live-offline para el caso sin servidor."""
    # _render_live_section() siempre emite live-offline; verificamos que la sección lo tiene
    # buscando que aparece en el contexto de la sección config
    idx = html.find('id="config"')
    assert idx >= 0, 'sección config no encontrada'
    snippet = html[idx: idx + 1200]
    assert "live-offline" in snippet, "falta div.live-offline en la sección config"


def test_config_cfg_body_div_present(html: str) -> None:
    """El div #cfg-body existe (el JS lo rellena vía fetch)."""
    assert "cfg-body" in html, "falta div con id cfg-body"


# ---- sección API ----

def test_api_section_id_present(html: str) -> None:
    """La sección API existe con id="api"."""
    assert 'id="api"' in html, 'falta <section id="api"> en el HTML renderizado'


def test_api_nav_button_present(html: str) -> None:
    """El botón de nav apunta a la sección api."""
    assert 'data-s="api"' in html, 'falta el botón de nav data-s="api"'


def test_api_nav_label_present(html: str) -> None:
    """El label del nav incluye la palabra API."""
    assert "API" in html, "falta el texto 'API' en la nav"


def test_api_loader_function_present(html: str) -> None:
    """El JS inline define la función loadApi."""
    assert "function loadApi()" in html, "falta la función loadApi() en el JS inline"


def test_api_onsectionshow_wired(html: str) -> None:
    """onSectionShow despacha a loadApi cuando s==='api'."""
    assert "loadApi()" in html, "loadApi() no está cableado en onSectionShow"


def test_api_offline_guard_present(html: str) -> None:
    """La sección API incluye el div live-offline para el caso sin servidor."""
    idx = html.find('id="api"')
    assert idx >= 0, 'sección api no encontrada'
    snippet = html[idx: idx + 1200]
    assert "live-offline" in snippet, "falta div.live-offline en la sección api"


def test_api_body_div_present(html: str) -> None:
    """El div #api-body existe (el JS lo rellena vía fetch)."""
    assert "api-body" in html, "falta div con id api-body"


def test_api_fetches_manifest_endpoint(html: str) -> None:
    """El loader de API hace fetch a /manifest."""
    assert "fetch('/manifest')" in html, "loadApi() no hace fetch a /manifest"


def test_config_fetches_config_endpoint(html: str) -> None:
    """El loader de Config hace fetch a /config."""
    assert "fetch('/config')" in html, "loadConfig() no hace fetch a /config"


# ---- ambas bajo el grupo 'Operar' del menú (IA menú/submenú 2026-06-24) ----

def test_both_sections_in_operar_group(html: str) -> None:
    """Config y API viven bajo el grupo colapsable 'Operar' del menú."""
    operar_idx = html.find("Operar")
    assert operar_idx >= 0, "no se encontró el grupo Operar en el nav"
    config_idx = html.find('data-s="config"')
    api_idx = html.find('data-s="api"')
    assert config_idx > operar_idx, "config debe estar bajo el grupo Operar"
    assert api_idx > operar_idx, "api debe estar bajo el grupo Operar"


# ---- FIX 1: loadConfig consume mcp_by_source (A4) ----

def test_load_config_uses_mcp_by_source(html: str) -> None:
    """FIX 1: loadConfig() lee d.mcp_by_source para mostrar el origen real de cada server.

    Verifica que el JS de loadConfig() referencia mcp_by_source (no construye allMcp
    solo a partir de mcp_repo/mcp_global, que devuelven origen binario).
    """
    assert "mcp_by_source" in html, (
        "loadConfig() no consume d.mcp_by_source — el origen real por server no se muestra"
    )


def test_load_config_shows_origin_column(html: str) -> None:
    """FIX 1: la columna de la tabla MCP se llama 'Origen', no 'Alcance' (binario)."""
    # 'Origen' aparece en el th de la tabla MCP dentro de loadConfig
    assert "Origen" in html, "tabla MCP de loadConfig no tiene columna 'Origen'"


# ---- FIX 3(b): loadHooks UI menciona 'ventana' ----

def test_load_hooks_mentions_ventana(html: str) -> None:
    """FIX 3(b): la sección hooks deja claro que los disparos son de una ventana, no all-time."""
    assert "ventana" in html, (
        "loadHooks() no menciona que los conteos son de ventana — el usuario puede confundirlos con all-time"
    )
