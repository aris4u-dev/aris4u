"""Guard de regresión: el JS inline que emite el render debe ser sintácticamente válido.

Un solo string JS mal escapado (p.ej. comilla simple dentro de un literal de comillas simples)
rompe TODO el render del panel — la página carga el sidebar pero el contenido queda en blanco,
y los tests de las funciones de datos (que prueban Python, no el JS emitido) no lo detectan.
Este test renderiza la consola completa, extrae los bloques <script> inline y los valida con
`node --check`. Habría atrapado el bug del 2026-06-23 (átomo 'used' en _ATYPE.hueco).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from aris4u_console import inventory, render_console

_CONSOLE = Path(__file__).resolve().parent.parent      # aris4u/console
_REPO = _CONSOLE.parent                                # aris4u
_CURATED = _CONSOLE / "aris4u_console" / "data" / "curated_semantics.json"


def _render() -> str:
    inv = inventory.build_inventory(_REPO, console_repo=_CONSOLE)
    curated = json.loads(_CURATED.read_text(encoding="utf-8")) if _CURATED.exists() else {}
    return render_console.render_console_html(inv, curated)


def test_inline_js_is_syntactically_valid() -> None:
    """Extrae los <script> sin src del HTML renderizado y los valida con node --check."""
    if not shutil.which("node"):
        pytest.skip("node no disponible para validar la sintaxis del JS")

    html = _render()
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    assert scripts, "el render no emitió ningún <script> inline — ¿cambió la estructura?"
    js = "\n;\n".join(scripts)

    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(js)
        path = fh.name
    try:
        proc = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=30)
    finally:
        Path(path).unlink(missing_ok=True)

    assert proc.returncode == 0, (
        f"El JS inline de la consola tiene un error de sintaxis (rompe el render):\n{proc.stderr}"
    )
