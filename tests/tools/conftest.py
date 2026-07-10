"""conftest.py — tests/tools/

CI-hermeticity: los tests de tag_observations_client dependen de un alias-map
que normalmente viene de ~/.aris4u/config.json. En un runner CI limpio ese
fichero no existe → _ALIAS = {} → _KNOWN_NAMES = [] → _RE_BROAD = None →
todos los tests de canonicalize_client / infer_client fallan.

La fixture _patch_toc_aliases inyecta el alias-map canónico completo en el
módulo (usando las 3 variables de módulo + _rebuild_patterns para recompilar
_RE_BROAD) antes de cada test de este subdirectorio. No toca el código de
producción ni tiene side-effects fuera del test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# -----------------------------------------------------------------------
# Bootstrap: añadir tools/ al path igual que hace el test hermano.
# -----------------------------------------------------------------------
_TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import tag_observations_client as toc  # noqa: E402

# -----------------------------------------------------------------------
# Alias-map canónico (espejo de ~/.aris4u/config.json "client_aliases").
# Cubre exactamente lo que los tests de TestCanonicalizeClient,
# TestInferClientFromPaths y TestInferClient comprueban.
# -----------------------------------------------------------------------
_FIXTURE_ALIAS: dict[str, str] = {
    # client-c aliases
    "client-c-inventory": "client-c",
    "lab-project-2": "client-c",
    "client-c": "client-c",
    # lab-project-1 aliases
    "lab-project-1-app": "lab-project-1",
    "lab-project-1": "lab-project-1",
    "old-name-lab-1": "lab-project-1",
    # client-d aliases
    "client-d-co": "client-d",
    "client-d-artifact": "client-d",
    "client-d": "client-d",
    # client-a
    "client-a": "client-a",
    # client-b (post-suffix-strip the name is "client-b", so must be in alias)
    "client-b": "client-b",
    "client-b-platform": "client-b",
    # client-e
    "client-e": "client-e",
    # aris4u
    "aris4u": "aris4u",
    # lab-project-4 / lab-project-5 (generic lab aliases)
    "lab-project-4": "lab-project-4",
    "lab-project-5": "lab-project-5",
}


@pytest.fixture(autouse=True)
def _patch_toc_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inyecta alias-map fijo en tag_observations_client antes de cada test.

    Recompila _KNOWN_NAMES y _RE_BROAD con _rebuild_patterns (el refactor
    mínimo que el fix permite) para que sean coherentes con _ALIAS.
    El monkeypatch revierte automáticamente al salir del test.
    """
    known_names, re_broad = toc._rebuild_patterns(_FIXTURE_ALIAS)
    monkeypatch.setattr(toc, "_ALIAS", _FIXTURE_ALIAS)
    monkeypatch.setattr(toc, "_KNOWN_NAMES", known_names)
    monkeypatch.setattr(toc, "_RE_BROAD", re_broad)
