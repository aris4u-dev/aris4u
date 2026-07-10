"""Anti-drift del manifiesto: cada ruta de capacidad debe estar en ENDPOINTS.

El manifiesto (GET /manifest) es el acceso nativo de Claude a TODO ARIS4U. Si alguien añade
una ruta nueva a do_GET/do_POST y olvida documentarla en ENDPOINTS, el manifiesto miente y
Claude no la descubre. Este test extrae las rutas reales del código fuente y exige que cada
una (salvo assets estáticos / transporte PTY, lista explícita) tenga su entrada en ENDPOINTS.
"""
from __future__ import annotations

import re
from pathlib import Path

from aris4u_console import server

_SRC = Path(server.__file__).read_text(encoding="utf-8")

# Rutas que NO son superficie de capacidad: assets estáticos y transporte de bajo nivel.
_EXCLUDE = {
    "/", "/index.html", "/console.html", "/inventory.json",
    "/pty/stream", "/pty/start", "/pty/input", "/pty/resize",
}


def _routes_in_source() -> set[str]:
    """Extrae las rutas literales comparadas con `route ==` en do_GET/do_POST."""
    return set(re.findall(r'route == "(/[a-z0-9_/-]*)"', _SRC))


def test_every_capability_route_is_in_manifest() -> None:
    declared = {e["path"] for e in server.ENDPOINTS}
    routed = _routes_in_source() - _EXCLUDE
    missing = routed - declared
    assert not missing, f"rutas sin entrada en ENDPOINTS (manifiesto incompleto): {sorted(missing)}"


def test_manifest_entries_point_to_real_routes() -> None:
    """Ninguna entrada del manifiesto debe apuntar a una ruta inexistente (manifiesto stale)."""
    routed = _routes_in_source() | {"/code"}  # /code se sirve con query, está en source
    declared = {e["path"] for e in server.ENDPOINTS}
    phantom = declared - routed
    assert not phantom, f"ENDPOINTS apunta a rutas que no existen en el server: {sorted(phantom)}"


def test_manifest_entries_well_formed() -> None:
    for e in server.ENDPOINTS:
        assert e.get("path", "").startswith("/"), f"path inválido: {e}"
        assert e.get("method") in {"GET", "POST"}, f"method inválido: {e}"
        assert e.get("purpose"), f"falta purpose: {e['path']}"
