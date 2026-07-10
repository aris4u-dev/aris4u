#!/usr/bin/env python3
"""
Unit tests for H47 — Migration FK constraint semantic validation

Tests the new _check_fk_constraints() method in migration_linter.py which detects
FOREIGN KEY references to non-existent tables (tables not created in prior migrations).
"""

import json
import subprocess
from pathlib import Path

TOOLS = Path(__file__).parent.parent / "tools"
LINTER = TOOLS / "migration_linter.py"


class TestH47FKValidation:
    """H47: FOREIGN KEY constraint semantic validation."""

    def _run(self, target: Path):
        """Run linter on target (file or directory)."""
        return subprocess.run(
            ["python3", str(LINTER), str(target)],
            capture_output=True, text=True,
        )

    def _parse_findings(self, stdout: str) -> list:
        """Parse JSONL findings from stdout."""
        return [json.loads(ln) for ln in stdout.splitlines() if ln.strip().startswith("{")]

    def test_valid_fk_reference(self, tmp_path):
        """FK referencing a table created in a prior migration passes."""
        migrations = tmp_path / "migrations"
        migrations.mkdir()

        # V1: creates the parent table
        (migrations / "001_create_users.sql").write_text(
            "CREATE TABLE users (id BIGINT PRIMARY KEY, name VARCHAR(255));"
        )

        # V2: creates table with FK to users
        (migrations / "002_create_orders.sql").write_text(
            """
CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);
"""
        )

        r = self._run(migrations)
        findings = self._parse_findings(r.stdout)
        fk_errors = [f for f in findings if f["category"] == "fk_reference_missing_table"]
        assert len(fk_errors) == 0, f"Should not flag valid FK: {fk_errors}"

    def test_invalid_fk_reference(self, tmp_path):
        """FK referencing a non-existent table is flagged as error."""
        migrations = tmp_path / "migrations"
        migrations.mkdir()

        # V1: only users table
        (migrations / "001_create_users.sql").write_text(
            "CREATE TABLE users (id BIGINT PRIMARY KEY);"
        )

        # V2: FK to non-existent products table
        (migrations / "002_create_orders.sql").write_text(
            """
CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    product_id BIGINT NOT NULL,
    FOREIGN KEY (product_id) REFERENCES products(id)
);
"""
        )

        r = self._run(migrations)
        assert r.returncode == 1, "Should exit with error for missing FK table"
        findings = self._parse_findings(r.stdout)
        fk_errors = [f for f in findings if f["category"] == "fk_reference_missing_table"]
        assert len(fk_errors) == 1, f"Expected 1 FK error, got {len(fk_errors)}"
        assert "products" in fk_errors[0]["message"].lower()
        assert fk_errors[0]["severity"] == "error"

    def test_self_referential_fk(self, tmp_path):
        """FK referencing a table created in the same migration passes."""
        migrations = tmp_path / "migrations"
        migrations.mkdir()

        # Self-referential: categories references categories(parent_id)
        (migrations / "001_create_categories.sql").write_text(
            """
CREATE TABLE categories (
    id BIGINT PRIMARY KEY,
    parent_id BIGINT,
    FOREIGN KEY (parent_id) REFERENCES categories(id)
);
"""
        )

        r = self._run(migrations)
        findings = self._parse_findings(r.stdout)
        fk_errors = [f for f in findings if f["category"] == "fk_reference_missing_table"]
        assert len(fk_errors) == 0, f"Self-referential FK should pass: {fk_errors}"

    def test_multiple_fk_in_same_file(self, tmp_path):
        """Multiple FKs in same file — only missing ones are flagged."""
        migrations = tmp_path / "migrations"
        migrations.mkdir()

        (migrations / "001_create_base.sql").write_text(
            """
CREATE TABLE users (id BIGINT PRIMARY KEY);
CREATE TABLE products (id BIGINT PRIMARY KEY);
"""
        )

        # V2 has two FKs: one valid (users), one missing (suppliers)
        (migrations / "002_create_orders.sql").write_text(
            """
CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    user_id BIGINT,
    supplier_id BIGINT,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
);
"""
        )

        r = self._run(migrations)
        findings = self._parse_findings(r.stdout)
        fk_errors = [f for f in findings if f["category"] == "fk_reference_missing_table"]
        assert len(fk_errors) == 1
        assert "suppliers" in fk_errors[0]["message"].lower()

    def test_no_fk_migration(self, tmp_path):
        """Migration without FK constraints is not affected."""
        migrations = tmp_path / "migrations"
        migrations.mkdir()

        (migrations / "001_add_column.sql").write_text(
            "ALTER TABLE users ADD COLUMN email VARCHAR(255);"
        )

        r = self._run(migrations)
        findings = self._parse_findings(r.stdout)
        fk_errors = [f for f in findings if f["category"] == "fk_reference_missing_table"]
        assert len(fk_errors) == 0

    def test_fk_case_insensitive(self, tmp_path):
        """FK matching is case-insensitive (PostgreSQL treats table names as lowercase)."""
        migrations = tmp_path / "migrations"
        migrations.mkdir()

        (migrations / "001_create_users.sql").write_text(
            "CREATE TABLE users (id BIGINT PRIMARY KEY);"
        )

        # FK uses uppercase table name — should still match
        (migrations / "002_create_orders.sql").write_text(
            """
CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    user_id BIGINT,
    FOREIGN KEY (user_id) REFERENCES USERS(id)
);
"""
        )

        r = self._run(migrations)
        findings = self._parse_findings(r.stdout)
        fk_errors = [f for f in findings if f["category"] == "fk_reference_missing_table"]
        assert len(fk_errors) == 0, f"Case-insensitive FK should pass: {fk_errors}"

    def test_alter_table_add_constraint(self, tmp_path):
        """ALTER TABLE ADD CONSTRAINT ... FOREIGN KEY is also validated."""
        migrations = tmp_path / "migrations"
        migrations.mkdir()

        (migrations / "001_create_users.sql").write_text(
            "CREATE TABLE users (id BIGINT PRIMARY KEY);"
        )

        (migrations / "002_create_orders.sql").write_text(
            "CREATE TABLE orders (id BIGINT PRIMARY KEY, user_id BIGINT);"
        )

        # V3 adds FK via ALTER — to a missing table
        (migrations / "003_add_fk.sql").write_text(
            """
ALTER TABLE orders ADD CONSTRAINT fk_orders_vendors
  FOREIGN KEY (vendor_id) REFERENCES vendors(id);
"""
        )

        r = self._run(migrations)
        findings = self._parse_findings(r.stdout)
        fk_errors = [f for f in findings if f["category"] == "fk_reference_missing_table"]
        assert len(fk_errors) == 1
        assert "vendors" in fk_errors[0]["message"].lower()

    def test_complex_multi_column_fk(self, tmp_path):
        """FK with multiple columns (composite) is validated."""
        migrations = tmp_path / "migrations"
        migrations.mkdir()

        (migrations / "001_base.sql").write_text(
            """
CREATE TABLE tenants (id BIGINT PRIMARY KEY, name TEXT);
CREATE TABLE users (id BIGINT PRIMARY KEY, tenant_id BIGINT);
"""
        )

        # Composite FK: (tenant_id, user_id) references (tenants.id, users.id)
        # Only tenants exists, users exists → should pass
        (migrations / "002_orders.sql").write_text(
            """
CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    tenant_id BIGINT,
    assigned_to BIGINT,
    FOREIGN KEY (tenant_id, assigned_to) REFERENCES users(tenant_id, id)
);
"""
        )

        r = self._run(migrations)
        findings = self._parse_findings(r.stdout)
        fk_errors = [f for f in findings if f["category"] == "fk_reference_missing_table"]
        assert len(fk_errors) == 0, "Multi-column FK to existing table should pass"

    def test_error_line_numbers(self, tmp_path):
        """FK errors report correct line numbers."""
        migrations = tmp_path / "migrations"
        migrations.mkdir()

        (migrations / "001_create_users.sql").write_text(
            "CREATE TABLE users (id BIGINT PRIMARY KEY);"
        )

        # FK on line 4
        (migrations / "002_orders.sql").write_text(
            """CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    user_id BIGINT,
    FOREIGN KEY (user_id) REFERENCES products(id)
);
"""
        )

        r = self._run(migrations)
        findings = self._parse_findings(r.stdout)
        fk_errors = [f for f in findings if f["category"] == "fk_reference_missing_table"]
        assert len(fk_errors) == 1
        assert fk_errors[0]["line"] == 4, f"Expected line 4, got {fk_errors[0]['line']}"
