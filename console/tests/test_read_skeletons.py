"""read_skeletons: catálogo de plantillas reutilizables (lo que el build flow inyecta).

Lista los átomos con skeleton, agrupados por familia (artifact_type). Read-only sobre una
sessions.db sintética.
"""
from __future__ import annotations

import sqlite3

import pytest


from aris4u_console import live_data


@pytest.fixture
def repo_with_skeletons(tmp_path):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "sessions.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY, decision TEXT, problem_class TEXT, "
        "artifact_type TEXT, regime TEXT, skeleton TEXT, validity_domain TEXT, "
        "source_project TEXT, mem_type TEXT)"
    )
    rows = [
        ("ledger append-only", "", "ledger-append-only", "deterministic",
         "create table public.<mov> (...)\n-- trigger ...", "x", "client-c-inventory", "fact"),
        ("rls helper", "", "access-control", "deterministic",
         "create function auth_tenant_ids() ...", "y", "lab-project-1-app", "fact"),
        ("sin skeleton (no entra)", "", "access-control", "deterministic",
         "", "z", "lab-project-1-app", "fact"),
    ]
    con.executemany(
        "INSERT INTO decisions (decision, problem_class, artifact_type, regime, skeleton, "
        "validity_domain, source_project, mem_type) VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return tmp_path


def test_skeletons_lists_only_atoms_with_skeleton(repo_with_skeletons):
    out = live_data.read_skeletons(repo_with_skeletons)
    assert out["available"] is True
    assert out["total"] == 2  # el tercero (sin skeleton) no entra


def test_skeletons_grouped_by_family(repo_with_skeletons):
    out = live_data.read_skeletons(repo_with_skeletons)
    fams = {g["family"] for g in out["by_family"]}
    assert "ledger-append-only" in fams
    assert "access-control" in fams


def test_skeletons_item_has_code_and_lines(repo_with_skeletons):
    out = live_data.read_skeletons(repo_with_skeletons)
    ledger = next(g for g in out["by_family"] if g["family"] == "ledger-append-only")
    item = ledger["items"][0]
    assert "create table" in item["skeleton"]
    assert item["lines"] == 2  # 2 líneas en el skeleton de prueba


def test_skeletons_offline_when_no_db(tmp_path):
    assert live_data.read_skeletons(tmp_path)["available"] is False
