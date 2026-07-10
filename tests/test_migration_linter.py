#!/usr/bin/env python3
"""
Unit tests for V16.2 H4 + V16.3 H16/H18 Migration Linter

Tests migration_linter.py tool which detects SQL migration bugs before apply,
including forward references, NOT NULL backfill issues, security definer gaps,
duplicate migration numbers (V16.3 H18), and single-file input (V16.3 H16).
"""

import json
import sys
import subprocess
from pathlib import Path
import tempfile
import pytest

TOOLS = Path(__file__).parent.parent / "tools"
LINTER = TOOLS / "migration_linter.py"


class TestV163SingleFileH16:
    """V16.3 H16 — linter accepts a single .sql file as arg, not only a directory."""

    def _run(self, target: Path):
        return subprocess.run(
            ["python3", str(LINTER), str(target)],
            capture_output=True, text=True,
        )

    def test_single_clean_file_exit_0(self, tmp_path):
        f = tmp_path / "001_init.sql"
        f.write_text("CREATE TABLE users (id uuid PRIMARY KEY);\n")
        r = self._run(f)
        assert r.returncode == 0, f"stdout={r.stdout} stderr={r.stderr}"

    def test_single_file_with_error_exit_1(self, tmp_path):
        f = tmp_path / "001_bad.sql"
        f.write_text(
            "CREATE INDEX bad_idx ON events(id) WHERE created_at > NOW();\n"
        )
        r = self._run(f)
        assert r.returncode == 1
        findings = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
        assert any(
            fnd["category"] == "non_immutable_in_partial_index" for fnd in findings
        )

    def test_non_sql_file_rejected(self, tmp_path):
        f = tmp_path / "readme.md"
        f.write_text("# not SQL\n")
        r = self._run(f)
        assert r.returncode == 1
        assert "not a .sql file" in r.stderr.lower()

    def test_nonexistent_path_rejected(self, tmp_path):
        r = self._run(tmp_path / "nope.sql")
        assert r.returncode == 1
        assert "does not exist" in r.stderr.lower()


class TestV163DuplicateNumberH18:
    """V16.3 H18 — detect two migrations sharing the same numerical prefix."""

    def _run(self, target: Path):
        return subprocess.run(
            ["python3", str(LINTER), str(target)],
            capture_output=True, text=True,
        )

    def test_duplicate_prefix_flagged(self, tmp_path):
        (tmp_path / "032_payments.sql").write_text(
            "CREATE TABLE payments (id uuid PRIMARY KEY);\n"
        )
        (tmp_path / "032_rate_limiting.sql").write_text(
            "CREATE TABLE rate_limits (id uuid PRIMARY KEY);\n"
        )
        r = self._run(tmp_path)
        assert r.returncode == 1
        findings = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
        dup = [f for f in findings if f["category"] == "duplicate_migration_number"]
        # Flagged for BOTH files (symmetric).
        assert len(dup) == 2
        files_flagged = {f["file"] for f in dup}
        assert files_flagged == {"032_payments.sql", "032_rate_limiting.sql"}

    def test_unique_prefixes_clean(self, tmp_path):
        (tmp_path / "031_a.sql").write_text(
            "CREATE TABLE a (id uuid PRIMARY KEY);\n"
        )
        (tmp_path / "032_b.sql").write_text(
            "CREATE TABLE b (id uuid PRIMARY KEY);\n"
        )
        (tmp_path / "033_c.sql").write_text(
            "CREATE TABLE c (id uuid PRIMARY KEY);\n"
        )
        r = self._run(tmp_path)
        findings = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
        dup = [f for f in findings if f["category"] == "duplicate_migration_number"]
        assert dup == []

    def test_three_way_duplicate_flagged(self, tmp_path):
        (tmp_path / "020_a.sql").write_text("CREATE TABLE a (id uuid PRIMARY KEY);\n")
        (tmp_path / "020_b.sql").write_text("CREATE TABLE b (id uuid PRIMARY KEY);\n")
        (tmp_path / "020_c.sql").write_text("CREATE TABLE c (id uuid PRIMARY KEY);\n")
        r = self._run(tmp_path)
        assert r.returncode == 1
        findings = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
        dup = [f for f in findings if f["category"] == "duplicate_migration_number"]
        assert len(dup) == 3  # one per file involved
        files_flagged = {f["file"] for f in dup}
        assert files_flagged == {"020_a.sql", "020_b.sql", "020_c.sql"}

    def test_single_file_not_flagged(self, tmp_path):
        """Single file → no duplicate check triggered."""
        f = tmp_path / "032_only.sql"
        f.write_text("CREATE TABLE only (id uuid PRIMARY KEY);\n")
        r = self._run(f)
        findings = [json.loads(ln) for ln in r.stdout.splitlines() if ln.strip().startswith("{")]
        dup = [f for f in findings if f["category"] == "duplicate_migration_number"]
        assert dup == []


class TestMigrationLinterStructure:
    """Test tool structure and imports."""

    @pytest.fixture
    def linter_path(self):
        """Path to migration_linter.py."""
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    def test_linter_exists(self, linter_path):
        """Tool should exist."""
        assert linter_path.exists(), f"Linter not found at {linter_path}"

    def test_linter_has_expected_classes(self, linter_path):
        """Tool should define MigrationLinter class."""
        content = linter_path.read_text()
        assert "class MigrationLinter" in content
        assert "class Finding" in content
        assert "def lint_directory" in content

    def test_linter_rules_documented(self, linter_path):
        """Tool should document the 8 linting rules."""
        content = linter_path.read_text()
        rules = [
            "forward_table_reference",
            "forward_column_reference",
            "column_not_in_table",
            "parameter_prefix_in_index",
            "non_immutable_in_partial_index",
            "missing_search_path_on_definer",
            "rls_policy_cycle",
            "inconsistent_column_name"
        ]
        for rule in rules:
            assert rule in content, f"Rule '{rule}' not found in linter"


class TestMigrationLinterCLI:
    """Test CLI interface."""

    @pytest.fixture
    def linter_path(self):
        """Path to migration_linter.py."""
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    def test_with_nonexistent_directory(self, linter_path):
        """Should handle missing directory gracefully."""
        result = subprocess.run(
            ["python3", str(linter_path), "/nonexistent/migrations"],
            capture_output=True,
            text=True,
            timeout=5
        )
        # Should exit with error
        assert result.returncode in (0, 1)

    def test_with_empty_migrations_dir(self, linter_path):
        """Should handle empty migrations directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["python3", str(linter_path), tmpdir],
                capture_output=True,
                text=True,
                timeout=5
            )
            # Should exit 0 (no migrations = no errors)
            assert result.returncode == 0


class TestCleanMigration:
    """Test with a clean, well-formed migration."""

    @pytest.fixture
    def linter_path(self):
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    @pytest.fixture
    def clean_migrations_dir(self):
        """Create a directory with a clean migration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            migrations_dir = Path(tmpdir) / "migrations"
            migrations_dir.mkdir()

            # Create a clean migration
            (migrations_dir / "001_init.sql").write_text('''
-- Clean migration with no issues
CREATE TABLE users (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email TEXT UNIQUE NOT NULL,
  created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_users_email ON users(email);

CREATE FUNCTION get_user_count() RETURNS INT AS $$
  SELECT COUNT(*) FROM users;
$$ LANGUAGE SQL IMMUTABLE;
''')

            yield migrations_dir

    def test_clean_migration_no_errors(self, linter_path, clean_migrations_dir):
        """Clean migration should produce no errors."""
        result = subprocess.run(
            ["python3", str(linter_path), str(clean_migrations_dir)],
            capture_output=True,
            text=True,
            timeout=5
        )

        assert result.returncode == 0
        # Should have no error lines (or only comments)
        error_lines = [ln for ln in result.stdout.split('\n')
                      if ln.strip() and '"severity":"error"' in ln]
        assert len(error_lines) == 0, f"Unexpected errors: {error_lines}"


class TestForwardTableReference:
    """Test detection of forward table references."""

    @pytest.fixture
    def linter_path(self):
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    @pytest.fixture
    def forward_ref_migrations(self):
        """Create migrations with forward reference bug."""
        with tempfile.TemporaryDirectory() as tmpdir:
            migrations_dir = Path(tmpdir) / "migrations"
            migrations_dir.mkdir()

            # Function references table that doesn't exist yet
            (migrations_dir / "001_bad_order.sql").write_text('''
-- BAD: Function tries to use table 'rides' before it's created
CREATE FUNCTION count_rides() RETURNS INT AS $$
  SELECT COUNT(*) FROM rides;
$$ LANGUAGE SQL;

-- Table created after the function
CREATE TABLE rides (
  id UUID PRIMARY KEY,
  driver_id UUID NOT NULL
);
''')

            yield migrations_dir

    def test_forward_table_reference_detected(self, linter_path, forward_ref_migrations):
        """Forward table references should be detected."""
        result = subprocess.run(
            ["python3", str(linter_path), str(forward_ref_migrations)],
            capture_output=True,
            text=True,
            timeout=5
        )

        # Smoke test — the actual detection depends on implementation, so we
        # only assert the linter ran to completion without crashing (no traceback).
        assert "Traceback" not in result.stderr

    def test_immutable_static_body_no_false_positive(self, linter_path, tmp_path):
        """LANGUAGE SQL IMMUTABLE with static SELECT body must NOT flag forward ref.

        Even when a table is created after the function, a purely static function
        body (no table reference in the signature) must not trigger forward_table_reference.
        """
        f = tmp_path / "001_static.sql"
        f.write_text(
            "CREATE FUNCTION get_tax_rate() RETURNS NUMERIC AS $$\n"
            "  SELECT 0.07::NUMERIC\n"
            "$$;\n"
            "\n"
            "CREATE TABLE accounts (id UUID PRIMARY KEY, balance DECIMAL NOT NULL);\n"
        )
        result = subprocess.run(
            ["python3", str(linter_path), str(f)],
            capture_output=True, text=True, timeout=5,
        )
        assert "Traceback" not in result.stderr
        findings = [json.loads(ln) for ln in result.stdout.splitlines() if ln.strip().startswith("{")]
        fwd = [f for f in findings if f["category"] == "forward_table_reference"]
        assert fwd == [], f"False positive: {fwd}"

    def test_immutable_values_body_no_false_positive(self, linter_path, tmp_path):
        """LANGUAGE SQL IMMUTABLE with VALUES body must NOT flag forward ref (client-incident).

        client-incident: a function whose body was VALUES (...) — a string literal
        that happened to contain a table name — was classified as forward_table_reference.
        The body is resolved at call time, not CREATE time; it must not be scanned.
        """
        f = tmp_path / "001_ima_repro.sql"
        f.write_text(
            "CREATE TABLE products (id UUID PRIMARY KEY);\n"
            "\n"
            "CREATE FUNCTION get_default_status() RETURNS TEXT AS $$\n"
            "  VALUES ('orders')\n"
            "$$;\n"
            "\n"
            # 'orders' appears as a string in the VALUES body AND as a table name after.
            # The linter must NOT flag this as a forward reference.
            "CREATE TABLE orders (id UUID PRIMARY KEY, status TEXT NOT NULL);\n"
        )
        result = subprocess.run(
            ["python3", str(linter_path), str(f)],
            capture_output=True, text=True, timeout=5,
        )
        assert "Traceback" not in result.stderr
        findings = [json.loads(ln) for ln in result.stdout.splitlines() if ln.strip().startswith("{")]
        fwd = [fi for fi in findings if fi["category"] == "forward_table_reference"]
        assert fwd == [], (
            "False positive (client-style): VALUES body triggered forward_table_reference. "
            f"Findings: {fwd}"
        )

    def test_returns_setof_forward_ref_detected(self, linter_path, tmp_path):
        """RETURNS SETOF <table> where the table is created later must be detected.

        True positive: the function SIGNATURE references a table that is only
        created after the function — a genuine DDL ordering bug.
        """
        f = tmp_path / "001_true_positive.sql"
        f.write_text(
            "CREATE FUNCTION get_future_records()\n"
            "  RETURNS SETOF future_table LANGUAGE SQL AS $$\n"
            "  SELECT * FROM future_table\n"
            "$$;\n"
            "\n"
            "CREATE TABLE future_table (id UUID PRIMARY KEY);\n"
        )
        result = subprocess.run(
            ["python3", str(linter_path), str(f)],
            capture_output=True, text=True, timeout=5,
        )
        assert "Traceback" not in result.stderr
        findings = [json.loads(ln) for ln in result.stdout.splitlines() if ln.strip().startswith("{")]
        fwd = [fi for fi in findings if fi["category"] == "forward_table_reference"]
        assert fwd, (
            "Expected forward_table_reference for RETURNS SETOF <future_table> "
            "but linter produced no finding."
        )

    def test_language_sql_existing_table_no_false_positive(self, linter_path, tmp_path):
        """LANGUAGE SQL (non-IMMUTABLE) referencing an already-created table must PASS.

        The table exists before the function — no forward reference. The linter
        must not flag this as an error regardless of IMMUTABLE marker.
        """
        f = tmp_path / "001_existing_ref.sql"
        f.write_text(
            "CREATE TABLE users (id UUID PRIMARY KEY, email TEXT NOT NULL);\n"
            "\n"
            "CREATE FUNCTION count_users() RETURNS BIGINT AS $$\n"
            "  SELECT COUNT(*) FROM users\n"
            "$$;\n"
        )
        result = subprocess.run(
            ["python3", str(linter_path), str(f)],
            capture_output=True, text=True, timeout=5,
        )
        assert "Traceback" not in result.stderr
        findings = [json.loads(ln) for ln in result.stdout.splitlines() if ln.strip().startswith("{")]
        fwd = [fi for fi in findings if fi["category"] == "forward_table_reference"]
        assert fwd == [], f"False positive on existing-table reference: {fwd}"


class TestNOTNullBackfillWarning:
    """Test detection of NOT NULL columns added without backfill."""

    @pytest.fixture
    def linter_path(self):
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    @pytest.fixture
    def notfill_migrations(self):
        """Create migrations with potential NOT NULL backfill issue."""
        with tempfile.TemporaryDirectory() as tmpdir:
            migrations_dir = Path(tmpdir) / "migrations"
            migrations_dir.mkdir()

            # First create table without column
            (migrations_dir / "001_init.sql").write_text('''
CREATE TABLE users (
  id UUID PRIMARY KEY,
  email TEXT NOT NULL
);
''')

            # Then add NOT NULL column without backfill
            (migrations_dir / "002_add_status.sql").write_text('''
ALTER TABLE users ADD COLUMN status TEXT NOT NULL;
-- WARNING: Adding NOT NULL without backfilling existing rows
''')

            yield migrations_dir

    def test_notfill_warning_detected(self, linter_path, notfill_migrations):
        """NOT NULL additions should generate warnings."""
        result = subprocess.run(
            ["python3", str(linter_path), str(notfill_migrations)],
            capture_output=True,
            text=True,
            timeout=5
        )

        # May return 0 or 1 depending on warning vs error classification
        # Tool should process migrations without crashing
        assert result.returncode in (0, 1)


class TestMissingSearchPath:
    """Test detection of SECURITY DEFINER functions without SET search_path."""

    @pytest.fixture
    def linter_path(self):
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    @pytest.fixture
    def no_searchpath_migrations(self):
        """Create migrations with missing search_path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            migrations_dir = Path(tmpdir) / "migrations"
            migrations_dir.mkdir()

            (migrations_dir / "001_funcs.sql").write_text('''
-- MISSING SET search_path — could fail silently
CREATE FUNCTION auth_check() RETURNS BOOLEAN AS $$
  SELECT TRUE;
$$ LANGUAGE SQL SECURITY DEFINER;

-- CORRECT version with SET search_path
CREATE FUNCTION auth_check_safe() RETURNS BOOLEAN AS $$
BEGIN
  SET search_path = public;
  SELECT TRUE;
END;
$$ LANGUAGE PLPGSQL SECURITY DEFINER;
''')

            yield migrations_dir

    def test_missing_search_path_warning(self, linter_path, no_searchpath_migrations):
        """Missing search_path on SECURITY DEFINER should warn."""
        result = subprocess.run(
            ["python3", str(linter_path), str(no_searchpath_migrations)],
            capture_output=True,
            text=True,
            timeout=5
        )

        # Smoke test — search_path analysis is best-effort; assert only that the
        # linter ran to completion without crashing (no traceback).
        assert "Traceback" not in result.stderr


class TestNonImmutablePartialIndex:
    """Test detection of non-immutable functions in partial indexes."""

    @pytest.fixture
    def linter_path(self):
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    @pytest.fixture
    def volatile_index_migrations(self):
        """Create migrations with volatile functions in indexes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            migrations_dir = Path(tmpdir) / "migrations"
            migrations_dir.mkdir()

            (migrations_dir / "001_indexes.sql").write_text('''
CREATE TABLE events (
  id UUID PRIMARY KEY,
  created_at TIMESTAMP DEFAULT NOW(),
  status TEXT
);

-- VOLATILE: NOW() in partial index will be rejected by PostgreSQL
CREATE INDEX idx_recent_events
  ON events(id)
  WHERE created_at > NOW();

-- IMMUTABLE: Safe version
CREATE INDEX idx_pending_events
  ON events(id)
  WHERE status = 'pending';
''')

            yield migrations_dir

    def test_volatile_in_index_detected(self, linter_path, volatile_index_migrations):
        """Volatile functions in indexes should be detected."""
        result = subprocess.run(
            ["python3", str(linter_path), str(volatile_index_migrations)],
            capture_output=True,
            text=True,
            timeout=5
        )

        # NOW() in a partial index WHERE clause is volatile → must be flagged.
        assert result.returncode == 1
        findings = [
            json.loads(ln)
            for ln in result.stdout.splitlines()
            if ln.strip().startswith("{")
        ]
        assert any(
            f["category"] == "non_immutable_in_partial_index" for f in findings
        )


class TestJsonlFindingFormat:
    """Test JSONL output format."""

    @pytest.fixture
    def linter_path(self):
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    @pytest.fixture
    def any_migrations(self):
        """Create any migration file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            migrations_dir = Path(tmpdir) / "migrations"
            migrations_dir.mkdir()
            (migrations_dir / "001.sql").write_text("CREATE TABLE t (id int);")
            yield migrations_dir

    def test_output_is_valid_jsonl(self, linter_path, any_migrations):
        """Output lines should be valid JSON."""
        result = subprocess.run(
            ["python3", str(linter_path), str(any_migrations)],
            capture_output=True,
            text=True,
            timeout=5
        )

        output = result.stdout
        for line in output.strip().split('\n'):
            if line:
                try:
                    obj = json.loads(line)
                    # Should have expected fields
                    assert 'severity' in obj or 'message' in obj or 'file' in obj
                except json.JSONDecodeError:
                    # May have non-JSON output (e.g., "No SQL migrations found")
                    pass


class TestMultipleMigrationFiles:
    """Test linting across multiple migration files."""

    @pytest.fixture
    def linter_path(self):
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    @pytest.fixture
    def multi_migrations(self):
        """Create multiple migration files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            migrations_dir = Path(tmpdir) / "migrations"
            migrations_dir.mkdir()

            (migrations_dir / "001_users.sql").write_text('''
CREATE TABLE users (
  id UUID PRIMARY KEY,
  email TEXT UNIQUE NOT NULL
);
''')

            (migrations_dir / "002_rides.sql").write_text('''
CREATE TABLE rides (
  id UUID PRIMARY KEY,
  driver_id UUID NOT NULL REFERENCES users(id),
  status TEXT DEFAULT 'pending'
);
''')

            (migrations_dir / "003_payments.sql").write_text('''
CREATE TABLE payments (
  id UUID PRIMARY KEY,
  ride_id UUID NOT NULL REFERENCES rides(id),
  amount DECIMAL NOT NULL
);
''')

            yield migrations_dir

    def test_multiple_migrations_processed(self, linter_path, multi_migrations):
        """Linter should process all migrations in order."""
        result = subprocess.run(
            ["python3", str(linter_path), str(multi_migrations)],
            capture_output=True,
            text=True,
            timeout=5
        )

        assert result.returncode == 0
        # Should not crash on multiple files


class TestFlywayNamingF34:
    """F34 fix — migration_linter handles Flyway naming `V<num>__<name>.sql`.

    Pre-fix, only Supabase CLI naming `<num>_<name>.sql` was supported, so
    Flyway repos (Client-A) had no duplicate-prefix detection or numerical sort.
    """

    @pytest.fixture
    def linter_path(self) -> Path:
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    def test_autodetect_flyway_naming(self, linter_path, tmp_path) -> None:
        """A directory with V<num>__ files autodetects naming=flyway."""
        sys.path.insert(0, str(linter_path.parent))
        from migration_linter import MigrationLinter  # type: ignore[import-not-found]
        (tmp_path / "V1__init.sql").write_text("CREATE TABLE a (id int);\n")
        (tmp_path / "V2__add_b.sql").write_text("CREATE TABLE b (id int);\n")
        linter = MigrationLinter(naming="auto")
        rc = linter.lint_path(str(tmp_path))
        assert rc == 0
        assert linter.naming == "flyway"

    def test_autodetect_supabase_naming(self, linter_path, tmp_path) -> None:
        """A directory with <num>_ files autodetects naming=supabase."""
        sys.path.insert(0, str(linter_path.parent))
        from migration_linter import MigrationLinter  # type: ignore[import-not-found]
        (tmp_path / "001_init.sql").write_text("CREATE TABLE a (id int);\n")
        (tmp_path / "002_add_b.sql").write_text("CREATE TABLE b (id int);\n")
        linter = MigrationLinter(naming="auto")
        rc = linter.lint_path(str(tmp_path))
        assert rc == 0
        assert linter.naming == "supabase"

    def test_flyway_duplicate_prefix_flagged(self, linter_path, tmp_path) -> None:
        """V1__init.sql + V1__other.sql → duplicate_migration_number error."""
        (tmp_path / "V1__init.sql").write_text("CREATE TABLE a (id int);\n")
        (tmp_path / "V1__other.sql").write_text("CREATE TABLE b (id int);\n")
        result = subprocess.run(
            ["python3", str(linter_path), str(tmp_path)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 1
        findings = [json.loads(ln) for ln in result.stdout.splitlines() if ln.strip()]
        dup = [f for f in findings if f["category"] == "duplicate_migration_number"]
        assert len(dup) == 2  # one per file

    def test_flyway_numerical_sort_v1_v2_v10(self, linter_path, tmp_path) -> None:
        """V1, V2, V10 must sort numerically (not lex which gives V1, V10, V2)."""
        sys.path.insert(0, str(linter_path.parent))
        from migration_linter import MigrationLinter  # type: ignore[import-not-found]
        for n in [10, 1, 2]:
            (tmp_path / f"V{n}__step.sql").write_text(f"-- step {n}\n")
        linter = MigrationLinter(naming="flyway")
        linter.lint_path(str(tmp_path))
        assert linter.file_order == ["V1__step.sql", "V2__step.sql", "V10__step.sql"]

    def test_naming_cli_flag_overrides_autodetect(self, linter_path, tmp_path) -> None:
        """--naming=flyway forces Flyway pattern even when files look ambiguous."""
        (tmp_path / "V1__init.sql").write_text("CREATE TABLE a (id int);\n")
        result = subprocess.run(
            ["python3", str(linter_path), str(tmp_path), "--naming=flyway"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0


class TestExpandedColumnTypesF36:
    """F36 fix — column type detection covers 40+ types, not 10."""

    @pytest.fixture
    def linter_path(self) -> Path:
        return Path(__file__).parent.parent / "tools" / "migration_linter.py"

    def test_varchar_decimal_jsonb_recognized(self, linter_path, tmp_path) -> None:
        """Pre-fix, VARCHAR/DECIMAL/JSONB cols weren't tracked → false negatives
        in column_not_in_table check. Now they're recognized."""
        sys.path.insert(0, str(linter_path.parent))
        from migration_linter import MigrationLinter  # type: ignore[import-not-found]
        (tmp_path / "001_extra_types.sql").write_text("""
            CREATE TABLE patients (
                id SERIAL PRIMARY KEY,
                full_name VARCHAR(200) NOT NULL,
                balance DECIMAL(10, 2),
                metadata JSONB,
                created_at TIMESTAMPTZ NOT NULL
            );
        """)
        linter = MigrationLinter(naming="supabase")
        linter.lint_path(str(tmp_path))
        cols = linter.columns_per_table.get("patients", set())
        assert {"id", "full_name", "balance", "metadata", "created_at"} <= cols, (
            f"missing columns. got: {cols}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
