"""Tests del ledger multiplicador ARIS4U (revenue_ledger.py).

3 tests principales (cada uno delegado a helpers focalizados para mantener CC baja):
  T1 — corre sin error sobre la DB real; todas las métricas son números sanos.
  T2 — la DB real NO muta después de correr el tool (read-only verificado).
  T3 — funciona sobre una DB temporal aislada (valores exactos conocidos).
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import revenue_ledger as rl  # noqa: E402

REAL_DB = Path(__file__).resolve().parent.parent.parent / "data" / "sessions.db"


def _real_db_ready() -> bool:
    """Returns True only when sessions.db exists AND has the recall_events table."""
    if not REAL_DB.exists():
        return False
    try:
        import sqlite3 as _sqlite3
        con = _sqlite3.connect(str(REAL_DB))
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        con.close()
        return "recall_events" in tables
    except Exception:
        return False


# Marker evaluated at COLLECTION TIME — may be stale if sessions.db is created
# by other tests at runtime. Tests decorated with this also check _real_db_ready()
# at the start of the test body for runtime safety.
_REAL_DB_MARK = pytest.mark.skipif(
    not REAL_DB.exists(),
    reason="sessions.db no encontrado",
)

# ---------------------------------------------------------------------------
# Helpers de sanidad por métrica (mantienen CC < 10 por función)
# ---------------------------------------------------------------------------


def _assert_m1_sane(m1: dict) -> None:
    assert m1["total_decisions"] >= 0
    assert m1["self_total"] + m1["revenue_total"] + m1["other_total"] <= m1["total_decisions"]
    assert 0.0 <= m1["self_pct_total"] <= 100.0
    assert 0.0 <= m1["revenue_pct_total"] <= 100.0
    assert m1["self_pct_total"] + m1["revenue_pct_total"] <= 100.0
    valid = {"↑", "→", "↓", "?"}
    assert m1["verdict_self"] in valid
    assert m1["verdict_revenue"] in valid


def _assert_m2_sane(m2: dict) -> None:
    assert m2["total"] >= 0
    assert m2["tagged"] <= m2["total"]
    assert 0.0 <= m2["fill_pct"] <= 100.0
    assert m2["verdict"] in {"↑", "→", "↓", "?"}


def _assert_m3_sane(m3: dict) -> None:
    assert m3["total"] >= 0
    assert m3["useful"] <= m3["total"]
    assert 0.0 <= m3["global_pct"] <= 100.0
    assert m3["revenue_total"] >= 0
    assert m3["revenue_useful"] <= m3["revenue_total"]


def _assert_m4_sane(m4: dict) -> None:
    for c in rl.REVENUE_CLIENTS:
        assert m4["by_client"][c] >= 0
        assert m4["by_client_30d"][c] >= 0
        assert m4["by_client_30d"][c] <= m4["by_client"][c]


def _assert_m5_sane(m5: dict) -> None:
    assert isinstance(m5["signal_available"], bool)
    assert m5["total_blocks"] >= 0


# ---------------------------------------------------------------------------
# Helpers de valores exactos (T3)
# ---------------------------------------------------------------------------


def _assert_m1_exact(m1: dict) -> None:
    assert m1["total_decisions"] == 8
    assert m1["self_total"] == 4
    assert m1["revenue_total"] == 3
    assert m1["other_total"] == 1
    assert m1["self_pct_total"] == 50.0
    assert m1["revenue_pct_total"] == 37.5


def _assert_m2_exact(m2: dict) -> None:
    assert m2["total"] == 4
    assert m2["tagged"] == 2
    assert m2["fill_pct"] == 50.0
    assert m2["session_start_total"] == 2
    assert m2["session_start_tagged"] == 1
    assert m2["user_prompt_total"] == 2
    assert m2["user_prompt_tagged"] == 1


def _assert_m3_exact(m3: dict) -> None:
    assert m3["total"] == 2
    assert m3["useful"] == 1
    assert m3["global_pct"] == 50.0
    assert m3["revenue_total"] == 2
    assert m3["revenue_useful"] == 1
    assert m3["revenue_pct"] == 50.0


def _assert_m4_exact(m4: dict) -> None:
    assert m4["by_client"]["client-b"] == 1
    assert m4["by_client"]["client-c"] == 1
    assert m4["by_client"]["client-a"] == 1
    assert m4["by_client"]["client-d"] == 0
    assert m4["by_client"]["client-e"] == 0


def _assert_m5_no_signal(m5: dict) -> None:
    assert m5["signal_available"] is False
    assert "session_ref" in m5["note"]


# ---------------------------------------------------------------------------
# T1 — sanidad sobre DB real
# ---------------------------------------------------------------------------


@_REAL_DB_MARK
def test_metrics_run_and_are_sane() -> None:
    """build_report() sobre la DB real devuelve métricas con valores sanos."""
    if not _real_db_ready():
        pytest.skip("sessions.db sin recall_events (DB nueva o no inicializada con datos de sesión)")
    data = rl.build_report(db=REAL_DB)
    _assert_m1_sane(data["m1"])
    _assert_m2_sane(data["m2"])
    _assert_m3_sane(data["m3"])
    _assert_m4_sane(data["m4"])
    _assert_m5_sane(data["m5"])


# ---------------------------------------------------------------------------
# T2 — la DB real no muta (checksum SHA-256)
# ---------------------------------------------------------------------------


@_REAL_DB_MARK
def test_no_db_mutation() -> None:
    """sessions.db no cambia tras ejecutar build_report()."""
    if not _real_db_ready():
        pytest.skip("sessions.db sin recall_events (DB nueva o no inicializada con datos de sesión)")

    def _sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    before = _sha(REAL_DB)
    rl.build_report(db=REAL_DB)
    assert _sha(REAL_DB) == before, "sessions.db fue modificado — el tool NO es read-only"


# ---------------------------------------------------------------------------
# T3 — DB aislada con valores controlados
# ---------------------------------------------------------------------------


def _make_test_db(tmp_path: Path) -> Path:
    """Crea una sessions.db mínima con datos controlados."""
    db = tmp_path / "sessions.db"
    conn = sqlite3.connect(str(db))
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.executemany(
        "INSERT INTO decisions (client_id) VALUES (?)",
        [
            ("aris4u",),
            ("aris4u",),
            ("lab-project-3",),
            ("",),
            ("client-b",),
            ("client-c",),
            ("client-a",),
            ("lab-project-1",),
        ],
    )

    cur.execute("""
        CREATE TABLE recall_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recall_id TEXT UNIQUE NOT NULL,
            ts TEXT NOT NULL,
            source TEXT DEFAULT 'user_prompt',
            client TEXT DEFAULT '',
            session_id TEXT DEFAULT '',
            project TEXT DEFAULT '',
            n_snippets INTEGER DEFAULT 0,
            query TEXT DEFAULT ''
        )
    """)
    cur.executemany(
        "INSERT INTO recall_events (recall_id, ts, source, client) VALUES (?,?,?,?)",
        [
            ("r1", "2026-07-05T10:00:00", "session_start", "client-b"),
            ("r2", "2026-07-05T10:01:00", "session_start", ""),
            ("r3", "2026-07-05T10:02:00", "user_prompt", "client-c"),
            ("r4", "2026-07-05T10:03:00", "user_prompt", ""),
        ],
    )

    cur.execute("""
        CREATE TABLE recall_feedback (
            recall_id TEXT PRIMARY KEY,
            useful INTEGER NOT NULL,
            marked_at TEXT NOT NULL,
            method TEXT DEFAULT 'manual',
            score REAL,
            detail TEXT
        )
    """)
    cur.executemany(
        "INSERT INTO recall_feedback (recall_id, useful, marked_at) VALUES (?,?,?)",
        [
            ("r1", 1, "2026-07-05T10:05:00"),
            ("r3", 0, "2026-07-05T10:06:00"),
        ],
    )

    cur.execute("""
        CREATE TABLE gate_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module_name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            e2e_prompt TEXT,
            session_ref TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    return db


def test_known_values_isolated_db(tmp_path: Path) -> None:
    """Con datos controlados, los cálculos baten exactamente los valores esperados."""
    db = _make_test_db(tmp_path)
    data = rl.build_report(db=db)

    _assert_m1_exact(data["m1"])
    _assert_m2_exact(data["m2"])
    _assert_m3_exact(data["m3"])
    _assert_m4_exact(data["m4"])
    _assert_m5_no_signal(data["m5"])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
