"""read_backlog: operacionaliza Valorización en un backlog de adopción VERIFICADO.

Proyecta los átomos adopt/build sobre sus proyectos destino, pero clasifica el FIT de cada
par (patrón, destino): absent / mismatch / likely-present / candidate. Por defecto solo surface
candidatos. Los tests monkeypatchean _project_profile para ser deterministas (no dependen de
qué repos cliente existan en disco) y fijan tanto el fit como el agrupado.
"""
from __future__ import annotations

import sqlite3

import pytest

from aris4u_console import live_data


# --- Perfil de proyecto stub (determinista) -------------------------------------------

# code -> (present, is_rls, migrations)
_FAKE_PROFILES = {
    "lab-project-1-app": (True, True, 86),    # present, RLS-mature
    "client-c": (True, True, 51),         # present, RLS-mature
    "client-e": (True, True, 84),          # present, RLS-mature
    "client-a": (True, False, 0),        # present, NO SQL (Astro)
    "client-d": (True, False, 0),      # present, NO SQL
    "client-b": (False, False, 0),   # stub / absent
}


@pytest.fixture(autouse=True)
def _fake_profiles(monkeypatch):
    monkeypatch.setattr(live_data, "_project_profile",
                        lambda code: _FAKE_PROFILES.get(code, (False, False, 0)))


# --- Lógica de fit --------------------------------------------------------------------

def test_fit_absent_when_target_not_present():
    assert live_data._fit_status("event-driven-state-machine", "client-b") == "absent"


def test_fit_mismatch_db_pattern_on_non_sql_target():
    # ledger-append-only requires DB; client-a has no migrations → mismatch
    assert live_data._fit_status("ledger-append-only", "client-a") == "mismatch"
    assert live_data._fit_status("access-control", "client-d") == "mismatch"


def test_fit_likely_present_rls_on_mature_target():
    assert live_data._fit_status("access-control", "client-c") == "likely-present"
    assert live_data._fit_status("multi-tenant-isolation", "client-e") == "likely-present"


def test_fit_candidate_app_pattern_on_present_target():
    # patrón app-level (no DB) en destino presente → candidate, aunque sea RLS-maduro
    assert live_data._fit_status("event-driven-state-machine", "client-c") == "candidate"
    assert live_data._fit_status("port-adapter-integration", "client-a") == "candidate"


# --- read_backlog: agrupado y filtrado ------------------------------------------------

@pytest.fixture
def repo_with_atoms(tmp_path):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "sessions.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE decisions (id INTEGER PRIMARY KEY, decision TEXT, problem_class TEXT, "
        "artifact_type TEXT, regime TEXT, skeleton TEXT, validity_domain TEXT, transfers_to TEXT, "
        "adoption TEXT, evidence_kind TEXT, source_project TEXT, structural_signature TEXT, "
        "mem_type TEXT, client_id TEXT)"
    )
    rows = [
        # FSM (app-level) de lab-project-1 → client-a(candidate) + client-b(absent) + client-c(candidate)
        ("proposal lifecycle fsm", "", "event-driven-state-machine", "deterministic", "",
         "aplica a flujos con estados; rompe sin transiciones", '["client-a","client-b","client-c"]',
         "used", "calibrated", "lab-project-1-app", "event-driven-state-machine|deterministic", "fact", ""),
        # ledger (DB) de lab-project-1 → client-a(mismatch, no SQL) + client-e(likely? no: ledger no es RLS_FAMILY → candidate)
        ("ledger append only", "", "ledger-append-only", "deterministic", "",
         "aplica a inventario; rompe con UPDATE", '["client-a","client-e"]',
         "used", "calibrated", "lab-project-1-app", "ledger-append-only|deterministic", "fact", ""),
    ]
    con.executemany(
        "INSERT INTO decisions (decision, problem_class, artifact_type, regime, skeleton, "
        "validity_domain, transfers_to, adoption, evidence_kind, source_project, "
        "structural_signature, mem_type, client_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return tmp_path


def test_backlog_only_shows_candidates_by_default(repo_with_atoms):
    out = live_data.read_backlog(repo_with_atoms)
    assert out["available"] is True
    # FSM→client-a, FSM→client-c, ledger→client-e = 3 candidates; client-b(absent) and client-a-ledger(mismatch) out
    assert out["total_items"] == 3
    ft = out["fit_totals"]
    assert ft.get("absent") == 1 and ft.get("mismatch") == 1 and ft.get("candidate") == 3


def test_backlog_filtered_projects_reports_reason(repo_with_atoms):
    out = live_data.read_backlog(repo_with_atoms)
    # client-b entirely discarded as absent (its only item, the FSM, is absent)
    assert "CLIENT-B" in {p.upper() for p in out["filtered_projects"]}


def test_backlog_items_sorted_by_score(repo_with_atoms):
    out = live_data.read_backlog(repo_with_atoms)
    for g in out["by_project"]:
        scores = [it["score"] for it in g["items"]]
        assert scores == sorted(scores, reverse=True)


def test_backlog_offline_when_no_db(tmp_path):
    assert live_data.read_backlog(tmp_path)["available"] is False
