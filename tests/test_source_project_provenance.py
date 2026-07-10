"""Contrato de provenance de átomos: source_project (Mejora #2, 2026-06-23).

save_decision auto-pobla source_project (carpeta del repo SIN canonicalizar) vía
detect_source_project (env → cwd → puente de sesión) para que los átomos FUTUROS lleven
origen sin backfill — el eje que alimenta el grafo de transferencia entre proyectos.
Distinto de client_id, que SÍ canonicaliza (client-b-platform → client-b).
"""
from __future__ import annotations

import sqlite3

import pytest

from engine.v16 import session_manager as sm


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """sessions.db aislada + embeddings stubbed (sin Ollama) para tests deterministas."""
    db = tmp_path / "sessions.db"
    monkeypatch.setattr(sm, "SESSIONS_DB", db)
    monkeypatch.setattr(sm, "_async_embed_decision", lambda *a, **k: None)
    sm.init_db()
    return db


def _read_source_project(db_path, decision_text):
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT source_project FROM decisions WHERE decision = ?", (decision_text,)
    ).fetchone()
    con.close()
    return row[0] if row else None


# --- detect_source_project: nombre EXACTO de carpeta, sin canonicalizar ---

@pytest.mark.parametrize("path,expected", [
    ("/Users/x/projects/aris4u/engine/v16", "aris4u"),
    ("/Users/x/projects/02-products/lab-project-1/lib/main.dart", "lab-project-1"),
    ("/Users/x/projects/client-b-platform/src", "client-b-platform"),
    ("/Users/x/projects/client-a", "client-a"),
    ("/tmp/fuera/de/projects", None),
])
def test_project_from_path(path, expected):
    assert sm._project_from_path(path) == expected


def test_detect_source_project_env_wins(monkeypatch):
    monkeypatch.setenv("ARIS4U_SOURCE_PROJECT", "  manual-proj  ")
    assert sm.detect_source_project("/Users/x/projects/aris4u") == "manual-proj"


def test_detect_source_project_falls_back_to_cwd(monkeypatch):
    monkeypatch.delenv("ARIS4U_SOURCE_PROJECT", raising=False)
    assert sm.detect_source_project("/Users/x/projects/02-products/lab-project-1") == "lab-project-1"


# --- save_decision: auto-población end-to-end ---

def test_save_decision_auto_tags_source_project(temp_db, monkeypatch):
    monkeypatch.setenv("ARIS4U_SOURCE_PROJECT", "lab-project-1")
    sm.save_decision(decision="surge control via token bucket", domain="ops")
    assert _read_source_project(temp_db, "surge control via token bucket") == "lab-project-1"


def test_save_decision_explicit_source_project_overrides_autodetect(temp_db, monkeypatch):
    monkeypatch.setenv("ARIS4U_SOURCE_PROJECT", "lab-project-1")
    sm.save_decision(decision="patrón explícito", source_project="client-a")
    assert _read_source_project(temp_db, "patrón explícito") == "client-a"


def test_save_decision_none_when_outside_projects(temp_db, monkeypatch):
    monkeypatch.delenv("ARIS4U_SOURCE_PROJECT", raising=False)
    monkeypatch.setattr(sm, "detect_source_project", lambda path=None: None)
    sm.save_decision(decision="sin origen detectable")
    assert _read_source_project(temp_db, "sin origen detectable") is None
