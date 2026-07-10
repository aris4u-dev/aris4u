"""Tests para tools/memory_revenue_meter.py.

Cubre:
  - Bucketing de client_id (incluyendo NULL, string vacío, valores desconocidos)
  - Cálculo de ratios y gap al target
  - Manejo de NULL en compute_ratio
  - aggregate_buckets a partir de tabla_counts compuestos
  - Persistencia de snapshots (in-memory SQLite, sin DB real)
  - Carga de snapshot previo (delta entre corridas)
  - CLI --json y --no-snapshot sobre DB temporal

Sin dependencia en la DB real de producción: todo usa SQLite in-memory o tmp_path.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

# Importar el módulo bajo test con path dinámico resuelto por conftest.
from tools.memory_revenue_meter import (
    BUCKET_LAB,
    BUCKET_NULL,
    BUCKET_OTHER,
    BUCKET_PENTEST,
    BUCKET_REVENUE,
    BUCKET_SELF,
    DEADLINE,
    MEMORY_TABLES,
    TARGET_RATIO,
    MeterReport,
    aggregate_buckets,
    classify_client,
    collect_counts,
    compute_ratio,
    days_to_deadline,
    ensure_snapshot_table,
    gap_to_target,
    load_previous_snapshot,
    main,
    save_snapshot,
)


# ---------------------------------------------------------------------------
# Fixtures de DB in-memory
# ---------------------------------------------------------------------------


def _make_memory_db() -> sqlite3.Connection:
    """Crea una DB in-memory con las 4 tablas de memoria pobladas con fixtures."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT
        );
        CREATE TABLE guards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT
        );
        CREATE TABLE digests (
            id TEXT PRIMARY KEY,
            client_id TEXT
        );
        CREATE TABLE observations_local (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT
        );
        """
    )
    return conn


def _insert_rows(conn: sqlite3.Connection, table: str, client_ids: list[str | None]) -> None:
    """Inserta filas con los client_id indicados en una tabla dada."""
    conn.executemany(
        f"INSERT INTO {table} (client_id) VALUES (?)",
        [(cid,) for cid in client_ids],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 1. Bucketing de client_id
# ---------------------------------------------------------------------------


class TestClassifyClient:
    """Verifica que classify_client asigna cada client_id al bucket correcto."""

    def test_revenue_clients(self) -> None:
        for cid in ["client-b", "client-c", "client-d", "client-e", "client-a", "acme-wellness"]:
            assert classify_client(cid) == BUCKET_REVENUE, f"falló para '{cid}'"

    def test_self_clients(self) -> None:
        for cid in ["aris4u", "lab-project-3"]:
            assert classify_client(cid) == BUCKET_SELF, f"falló para '{cid}'"

    def test_lab_clients(self) -> None:
        for cid in ["lab-project-1", "lab-project-1-legacy", "lab-project-2", "lab-project-4", "quimera"]:
            assert classify_client(cid) == BUCKET_LAB, f"falló para '{cid}'"

    def test_pentest_client(self) -> None:
        assert classify_client("pentest") == BUCKET_PENTEST

    def test_sql_null_is_null_bucket(self) -> None:
        assert classify_client(None) == BUCKET_NULL

    def test_empty_string_is_null_bucket(self) -> None:
        assert classify_client("") == BUCKET_NULL

    def test_whitespace_only_is_null_bucket(self) -> None:
        # strip() antes de lower() — un client_id de solo espacios → NULL
        assert classify_client("   ") == BUCKET_NULL

    def test_unknown_client_is_other(self) -> None:
        for cid in ["ue-mcp", "lab-project-5", "desconocido", "xyz-corp"]:
            assert classify_client(cid) == BUCKET_OTHER, f"falló para '{cid}'"

    def test_case_insensitive(self) -> None:
        assert classify_client("CLIENT-C") == BUCKET_REVENUE
        assert classify_client("LAB-PROJECT-1") == BUCKET_LAB
        assert classify_client("ARIS4U") == BUCKET_SELF


# ---------------------------------------------------------------------------
# 2. Cálculo de ratios
# ---------------------------------------------------------------------------


class TestComputeRatio:
    """Verifica compute_ratio con varios escenarios de conteos por bucket."""

    def test_all_revenue_no_null(self) -> None:
        counts = {BUCKET_REVENUE: 100}
        rt, rtotal = compute_ratio(counts)
        assert rt == pytest.approx(1.0)
        assert rtotal == pytest.approx(1.0)

    def test_half_revenue_no_null(self) -> None:
        counts = {BUCKET_REVENUE: 50, BUCKET_SELF: 50}
        rt, rtotal = compute_ratio(counts)
        assert rt == pytest.approx(0.5)
        assert rtotal == pytest.approx(0.5)

    def test_null_excluded_from_tagged_denominator(self) -> None:
        # 40 revenue, 60 self, 100 NULL → tagged=100, total=200
        counts = {BUCKET_REVENUE: 40, BUCKET_SELF: 60, BUCKET_NULL: 100}
        rt, rtotal = compute_ratio(counts)
        assert rt == pytest.approx(40 / 100)     # ratio_tagged = 40 %
        assert rtotal == pytest.approx(40 / 200)  # ratio_total  = 20 %

    def test_zero_total_returns_zeros(self) -> None:
        rt, rtotal = compute_ratio({})
        assert rt == pytest.approx(0.0)
        assert rtotal == pytest.approx(0.0)

    def test_all_null_returns_zero_tagged(self) -> None:
        counts = {BUCKET_NULL: 500}
        rt, rtotal = compute_ratio(counts)
        assert rt == pytest.approx(0.0)  # tagged=0 → 0/0 safe
        assert rtotal == pytest.approx(0.0)

    def test_no_revenue_returns_zero_ratio(self) -> None:
        counts = {BUCKET_SELF: 200, BUCKET_LAB: 100}
        rt, rtotal = compute_ratio(counts)
        assert rt == pytest.approx(0.0)
        assert rtotal == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 3. Gap al target y deadline
# ---------------------------------------------------------------------------


class TestGapAndDeadline:
    """Verifica gap_to_target y days_to_deadline."""

    def test_gap_below_target(self) -> None:
        gap = gap_to_target(0.10)
        assert gap == pytest.approx(TARGET_RATIO - 0.10)
        assert gap > 0  # todavía falta

    def test_gap_above_target(self) -> None:
        gap = gap_to_target(0.50)
        assert gap < 0  # ya superó el target

    def test_gap_at_target_is_zero(self) -> None:
        assert gap_to_target(TARGET_RATIO) == pytest.approx(0.0)

    def test_deadline_constant_format(self) -> None:
        assert DEADLINE == "2026-10-03"
        # Verifica que sea parseable como fecha ISO
        d = date.fromisoformat(DEADLINE)
        assert d.year == 2026

    def test_days_to_deadline_is_integer(self) -> None:
        days = days_to_deadline()
        assert isinstance(days, int)

    def test_days_to_deadline_computed_live(self) -> None:
        # No está hardcodeado: debe ser ≤ 89 días desde 2026-07-06
        days = days_to_deadline()
        deadline = date.fromisoformat(DEADLINE)
        expected = (deadline - date.today()).days
        assert days == expected


# ---------------------------------------------------------------------------
# 4. aggregate_buckets
# ---------------------------------------------------------------------------


class TestAggregateBuckets:
    """Verifica que aggregate_buckets suma correctamente a través de tablas."""

    def test_single_table(self) -> None:
        table_counts = {"decisions": {BUCKET_REVENUE: 10, BUCKET_SELF: 5}}
        result = aggregate_buckets(table_counts)
        assert result == {BUCKET_REVENUE: 10, BUCKET_SELF: 5}

    def test_multiple_tables_same_bucket(self) -> None:
        table_counts = {
            "decisions": {BUCKET_REVENUE: 10},
            "guards": {BUCKET_REVENUE: 5},
            "digests": {BUCKET_REVENUE: 2},
        }
        result = aggregate_buckets(table_counts)
        assert result[BUCKET_REVENUE] == 17

    def test_multiple_tables_different_buckets(self) -> None:
        table_counts = {
            "decisions": {BUCKET_REVENUE: 10, BUCKET_NULL: 3},
            "observations_local": {BUCKET_SELF: 20, BUCKET_NULL: 100},
        }
        result = aggregate_buckets(table_counts)
        assert result[BUCKET_REVENUE] == 10
        assert result[BUCKET_SELF] == 20
        assert result[BUCKET_NULL] == 103

    def test_empty_tables(self) -> None:
        assert aggregate_buckets({}) == {}


# ---------------------------------------------------------------------------
# 5. collect_counts sobre DB in-memory
# ---------------------------------------------------------------------------


class TestCollectCounts:
    """Verifica collect_counts leyendo una DB in-memory con fixtures conocidos."""

    def test_revenue_rows_land_in_revenue_bucket(self) -> None:
        conn = _make_memory_db()
        _insert_rows(conn, "decisions", ["client-c", "client-e", "client-d"])
        result = collect_counts(conn)
        assert result["decisions"][BUCKET_REVENUE] == 3
        conn.close()

    def test_null_rows_land_in_null_bucket(self) -> None:
        conn = _make_memory_db()
        _insert_rows(conn, "observations_local", [None, None, "", None])
        result = collect_counts(conn)
        # None (SQL NULL) y '' (string vacío) deben caer en NULL
        assert result["observations_local"][BUCKET_NULL] == 4
        conn.close()

    def test_mixed_distribution(self) -> None:
        conn = _make_memory_db()
        _insert_rows(conn, "decisions", ["aris4u", "aris4u", "client-c", None, "lab-project-1"])
        result = collect_counts(conn)
        bc = result["decisions"]
        assert bc[BUCKET_SELF] == 2
        assert bc[BUCKET_REVENUE] == 1
        assert bc[BUCKET_NULL] == 1
        assert bc[BUCKET_LAB] == 1
        conn.close()

    def test_all_memory_tables_present(self) -> None:
        conn = _make_memory_db()
        result = collect_counts(conn)
        for tbl in MEMORY_TABLES:
            assert tbl in result
        conn.close()

    def test_empty_table_returns_empty_bucket_map(self) -> None:
        conn = _make_memory_db()
        result = collect_counts(conn)
        assert result["decisions"] == {}
        conn.close()


# ---------------------------------------------------------------------------
# 6. Persistencia de snapshots
# ---------------------------------------------------------------------------


class TestSnapshotPersistence:
    """Verifica save_snapshot / load_previous_snapshot con DB in-memory."""

    def _fresh_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        ensure_snapshot_table(conn)
        return conn

    def test_save_returns_id(self) -> None:
        conn = self._fresh_conn()
        sid = save_snapshot(conn, 0.07, 0.04, {}, {})
        assert isinstance(sid, int)
        assert sid >= 1
        conn.close()

    def test_load_previous_snapshot_empty_db_returns_none(self) -> None:
        conn = self._fresh_conn()
        result = load_previous_snapshot(conn)
        assert result is None
        conn.close()

    def test_load_previous_snapshot_after_one_save(self) -> None:
        conn = self._fresh_conn()
        save_snapshot(conn, 0.07, 0.04, {"decisions": {BUCKET_REVENUE: 5}}, {BUCKET_REVENUE: 5})
        snap = load_previous_snapshot(conn)
        assert snap is not None
        assert snap["ratio_tagged"] == pytest.approx(0.07)
        assert snap["ratio_total"] == pytest.approx(0.04)
        conn.close()

    def test_load_previous_snapshot_returns_latest(self) -> None:
        conn = self._fresh_conn()
        save_snapshot(conn, 0.05, 0.03, {}, {})
        save_snapshot(conn, 0.12, 0.08, {}, {})
        snap = load_previous_snapshot(conn)
        assert snap is not None
        # Debe retornar el más reciente (0.12)
        assert snap["ratio_tagged"] == pytest.approx(0.12)
        conn.close()

    def test_snapshot_counts_json_round_trips(self) -> None:
        conn = self._fresh_conn()
        tc = {"decisions": {BUCKET_REVENUE: 42, BUCKET_NULL: 8}}
        bt = {BUCKET_REVENUE: 42, BUCKET_NULL: 8}
        save_snapshot(conn, 0.84, 0.70, tc, bt)
        snap = load_previous_snapshot(conn)
        assert snap is not None
        assert snap["counts_json"]["decisions"][BUCKET_REVENUE] == 42
        assert snap["bucket_totals_json"][BUCKET_NULL] == 8
        conn.close()

    def test_ensure_snapshot_table_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        ensure_snapshot_table(conn)
        ensure_snapshot_table(conn)  # Segunda llamada no debe lanzar
        conn.close()


# ---------------------------------------------------------------------------
# 7. MeterReport.delta_ratio_tagged
# ---------------------------------------------------------------------------


class TestMeterReportDelta:
    """Verifica el cálculo de delta entre corridas en MeterReport."""

    def _make_report(
        self,
        ratio_tagged: float,
        prev_ratio: float | None = None,
    ) -> MeterReport:
        prev = None
        if prev_ratio is not None:
            prev = {"ratio_tagged": prev_ratio, "ratio_total": 0.0}
        return MeterReport(
            run_ts="2026-07-06T00:00:00+00:00",
            deadline=DEADLINE,
            days_to_deadline=89,
            ratio_tagged=ratio_tagged,
            ratio_total=ratio_tagged / 2,
            gap_to_target=TARGET_RATIO - ratio_tagged,
            target_ratio=TARGET_RATIO,
            bucket_totals={},
            table_counts={},
            prev_snapshot=prev,
        )

    def test_delta_none_when_no_prev(self) -> None:
        r = self._make_report(0.07, prev_ratio=None)
        assert r.delta_ratio_tagged is None

    def test_delta_positive_improvement(self) -> None:
        r = self._make_report(0.10, prev_ratio=0.07)
        assert r.delta_ratio_tagged == pytest.approx(0.03)

    def test_delta_negative_regression(self) -> None:
        r = self._make_report(0.05, prev_ratio=0.08)
        assert r.delta_ratio_tagged == pytest.approx(-0.03)

    def test_as_dict_includes_delta(self) -> None:
        r = self._make_report(0.10, prev_ratio=0.07)
        d = r.as_dict()
        assert "delta_ratio_tagged" in d
        assert d["delta_ratio_tagged"] == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# 8. CLI integration (sin DB real)
# ---------------------------------------------------------------------------


class TestCLI:
    """Smoke tests del CLI con una DB temporal poblada con fixtures."""

    def _make_tmp_db(self, tmp_path: Path) -> Path:
        db = tmp_path / "sessions_test.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE decisions (id INTEGER PRIMARY KEY, client_id TEXT);
            CREATE TABLE guards (id INTEGER PRIMARY KEY, client_id TEXT);
            CREATE TABLE digests (id TEXT PRIMARY KEY, client_id TEXT);
            CREATE TABLE observations_local (id INTEGER PRIMARY KEY, client_id TEXT);
            INSERT INTO decisions (client_id) VALUES ('client-c'),('client-e'),('aris4u'),(NULL);
            INSERT INTO guards (client_id) VALUES ('client-b');
            INSERT INTO observations_local (client_id) VALUES (NULL),(NULL),('client-a');
            """
        )
        conn.commit()
        conn.close()
        return db

    def test_no_snapshot_flag_does_not_create_table(self, tmp_path: Path) -> None:
        db = self._make_tmp_db(tmp_path)
        rc = main(["--no-snapshot", "--db", str(db)])
        assert rc == 0
        conn = sqlite3.connect(str(db))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "memory_revenue_snapshots" not in tables

    def test_json_output_structure(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db = self._make_tmp_db(tmp_path)
        rc = main(["--json", "--no-snapshot", "--db", str(db)])
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "ratio_tagged" in data
        assert "ratio_total" in data
        assert "gap_to_target" in data
        assert "bucket_totals" in data
        assert "table_counts" in data
        assert "days_to_deadline" in data
        assert "deadline" in data

    def test_json_ratio_tagged_correct(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db = self._make_tmp_db(tmp_path)
        rc = main(["--json", "--no-snapshot", "--db", str(db)])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        # Fixtures: revenue = client-c + client-e + client-b + client-a = 4 rows
        # tagged = total - NULL; NULL en decisions=1, obs_local=2 → total=8, NULL=3, tagged=5
        assert data["ratio_tagged"] == pytest.approx(4 / 5, rel=1e-4)

    def test_snapshot_persisted_when_save_true(self, tmp_path: Path) -> None:
        db = self._make_tmp_db(tmp_path)
        rc = main(["--db", str(db)])
        assert rc == 0
        conn = sqlite3.connect(str(db))
        count = conn.execute("SELECT COUNT(*) FROM memory_revenue_snapshots").fetchone()[0]
        conn.close()
        assert count == 1

    def test_error_on_missing_db(self, tmp_path: Path) -> None:
        rc = main(["--db", str(tmp_path / "nonexistent.db")])
        assert rc == 1
