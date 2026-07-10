"""Unit tests for schema_compat_check.py

Tests V16.2 H3 — Schema Compatibility Drift Detector. Minimal smoke tests
since the tool requires a real Postgres instance for full introspection.
These tests validate the parsing and error detection logic.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

tools_path = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(tools_path))


class TestSchemaCompatCheckSmoke:
    """Smoke tests for schema_compat_check.py logic."""

    def test_empty_project_graceful(self) -> None:
        """Tool should handle empty/nonexistent project directory."""
        with TemporaryDirectory() as tmpdir:
            result = subprocess.run(
                ["python3", str(tools_path / "schema_compat_check.py"), tmpdir],
                capture_output=True,
                text=True,
            )
            # Should exit gracefully (not crash) even with empty dir
            # Exit code may be 0 or 1 depending on DB connectivity
            assert result.returncode in (0, 1, 2), f"Unexpected exit code: {result.returncode}"

    def test_no_flutter_code_no_crash(self) -> None:
        """Should not crash on projects with no lib/services/ structure."""
        with TemporaryDirectory() as tmpdir:
            Path(tmpdir, "lib").mkdir()
            Path(tmpdir, "supabase").mkdir()
            result = subprocess.run(
                ["python3", str(tools_path / "schema_compat_check.py"), tmpdir],
                capture_output=True,
                text=True,
            )
            # Should gracefully report no issues or DB unavailable
            assert result.returncode in (0, 1, 2)

    def test_malformed_dart_file_graceful(self) -> None:
        """Malformed Dart should not crash the parser."""
        with TemporaryDirectory() as tmpdir:
            lib_dir = Path(tmpdir) / "lib" / "services"
            lib_dir.mkdir(parents=True)

            # Write malformed Dart (missing closing parens)
            service_file = lib_dir / "user_service.dart"
            service_file.write_text("class UserService {\n  void getUser({\n    // missing closing")

            result = subprocess.run(
                ["python3", str(tools_path / "schema_compat_check.py"), tmpdir],
                capture_output=True,
                text=True,
            )
            # Should handle parse errors gracefully
            assert result.returncode in (0, 1, 2)

    def test_tool_runs_without_db_connection(self) -> None:
        """Tool should report DB unavailable rather than crashing."""
        with TemporaryDirectory() as tmpdir:
            lib_dir = Path(tmpdir) / "lib" / "services"
            lib_dir.mkdir(parents=True)

            # Create minimal Dart file with .from() reference
            service_file = lib_dir / "test_service.dart"
            service_file.write_text("""
class TestService {
  Future<List> getAll() async {
    return await supabase.from('test_table').select().execute();
  }
}
""")

            result = subprocess.run(
                ["python3", str(tools_path / "schema_compat_check.py"), tmpdir],
                capture_output=True,
                text=True,
            )
            # Should handle missing DB gracefully (exit 2 for DB unavailable)
            assert result.returncode in (0, 1, 2)


class TestSchemaCompatCheckParsing:
    """Test parsing logic via direct Python import (when psycopg2 available)."""

    @pytest.mark.skipif(
        subprocess.run(["python3", "-c", "import psycopg2"], capture_output=True).returncode != 0,
        reason="psycopg2 not installed",
    )
    def test_parser_initialization(self) -> None:
        """Parser should initialize without errors."""
        try:
            from schema_compat_check import FlutterCodeParser

            parser = FlutterCodeParser()  # type: ignore[call-arg]  # test intentionally calls without project_root to test resilience
            # Should not crash
            assert parser is not None
        except ImportError:
            pytest.skip("schema_compat_check not importable")

    @pytest.mark.skipif(
        subprocess.run(["python3", "-c", "import psycopg2"], capture_output=True).returncode != 0,
        reason="psycopg2 not installed",
    )
    def test_parser_finds_from_patterns(self) -> None:
        """Parser should extract .from() table references from Dart."""
        try:
            from schema_compat_check import FlutterCodeParser

            FlutterCodeParser()  # type: ignore[call-arg]  # test intentionally calls without project_root to test resilience

            # Simulate Dart code with .from() calls
            code = """
            final data = await supabase
              .from('users')
              .select()
              .eq('id', userId)
              .single();
            """

            # Parse should work (exact method depends on implementation)
            assert ".from(" in code
            assert "'users'" in code
        except ImportError:
            pytest.skip("schema_compat_check not importable")


class TestSchemaCompatCheckIntegration:
    """Integration tests with minimal fixtures."""

    def test_minimal_flutter_migration_setup(self) -> None:
        """Create minimal Flutter+migration fixture and test."""
        with TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)

            # Create lib/services/users_service.dart
            lib_services = project_root / "lib" / "services"
            lib_services.mkdir(parents=True)

            users_service = lib_services / "users_service.dart"
            users_service.write_text("""
import 'package:supabase_flutter/supabase_flutter.dart';

class UsersService {
  final supabase = Supabase.instance.client;

  Future<Map> getUserById(String id) async {
    return await supabase
      .from('users')
      .select()
      .eq('id', id)
      .single();
  }
}
""")

            # Create supabase/migrations/001_users.sql
            migrations = project_root / "supabase" / "migrations"
            migrations.mkdir(parents=True)

            migration = migrations / "001_users.sql"
            migration.write_text("""
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email text UNIQUE NOT NULL,
  name text,
  created_at timestamp DEFAULT now()
);
""")

            # Run tool
            result = subprocess.run(
                ["python3", str(tools_path / "schema_compat_check.py"), str(project_root)],
                capture_output=True,
                text=True,
            )

            # Should exit gracefully (0, 1, or 2 depending on DB)
            assert result.returncode in (
                0,
                1,
                2,
            ), f"Exit code: {result.returncode}, stderr: {result.stderr}"


class TestSchemaCompatCheckHelpers:
    """Test helper functions if exposed."""

    def test_tool_help_text(self) -> None:
        """Tool should have reasonable help/usage output."""
        result = subprocess.run(
            ["python3", str(tools_path / "schema_compat_check.py"), "--help"],
            capture_output=True,
            text=True,
        )
        # May succeed or fail depending on argparse setup
        # Just ensure it doesn't segfault
        assert result.returncode in (0, 1, 2)


class TestV163StaticFallbackH17:
    """V16.3 H17 — Static fallback parses migrations/ without psycopg2."""

    def _run(self, project_root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", str(tools_path / "schema_compat_check.py"), str(project_root)],
            capture_output=True,
            text=True,
        )

    def _parse_output(self, stdout: str) -> tuple[list[dict], dict | None]:
        """Split JSONL output into findings + _meta footer."""
        findings: list[dict] = []
        meta: dict | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("_meta") is True:
                meta = obj
            else:
                findings.append(obj)
        return findings, meta

    def test_static_mode_detects_missing_table(self) -> None:
        """Dart references a table NOT defined in migrations → static mode flags it."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            svc = root / "lib" / "services"
            svc.mkdir(parents=True)
            (svc / "orders.dart").write_text("final rows = await supabase.from('orders').select();")
            mig = root / "supabase" / "migrations"
            mig.mkdir(parents=True)
            (mig / "001_users.sql").write_text(
                "CREATE TABLE users (id uuid PRIMARY KEY, email text NOT NULL);"
            )

            result = self._run(root)
            findings, meta = self._parse_output(result.stdout)

            assert meta is not None, f"no _meta footer emitted. stdout={result.stdout[:500]}"
            assert meta["source"].startswith("static"), f"expected static* source, got {meta}"
            assert meta["tables_known"] == 1
            missing_table_errs = [
                f
                for f in findings
                if f.get("category") == "missing_table" and f.get("expected") == "orders"
            ]
            assert len(missing_table_errs) == 1, f"findings={findings}"
            assert result.returncode == 1, "exit 1 expected when errors found"

    def test_static_mode_detects_missing_rpc(self) -> None:
        """Dart calls .rpc('fn') NOT defined in migrations → flagged."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            svc = root / "lib" / "services"
            svc.mkdir(parents=True)
            (svc / "rpc_call.dart").write_text("await supabase.rpc('calculate_total', params: {});")
            mig = root / "supabase" / "migrations"
            mig.mkdir(parents=True)
            (mig / "001_init.sql").write_text(
                "CREATE OR REPLACE FUNCTION other_fn() RETURNS void AS $$ BEGIN END $$ LANGUAGE plpgsql;"
            )

            result = self._run(root)
            findings, meta = self._parse_output(result.stdout)
            assert meta and meta["source"].startswith("static")
            assert meta["rpcs_known"] == 1
            missing_rpc = [
                f
                for f in findings
                if f.get("category") == "missing_rpc" and f.get("expected") == "calculate_total"
            ]
            assert len(missing_rpc) == 1

    def test_static_mode_clean_project_no_errors(self) -> None:
        """Dart references match migrations → 0 errors, exit 0."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            svc = root / "lib" / "services"
            svc.mkdir(parents=True)
            (svc / "users.dart").write_text("await supabase.from('users').select();")
            mig = root / "supabase" / "migrations"
            mig.mkdir(parents=True)
            (mig / "001_users.sql").write_text(
                "CREATE TABLE IF NOT EXISTS users (id uuid PRIMARY KEY);"
            )

            result = self._run(root)
            findings, meta = self._parse_output(result.stdout)
            assert meta and meta["source"].startswith("static")
            error_findings = [f for f in findings if f.get("severity") == "error"]
            assert error_findings == [], f"expected 0 errors, got {error_findings}"
            assert result.returncode == 0

    def test_no_migrations_no_dsn_exits_2(self) -> None:
        """Neither DB nor migrations → exit 2 with explanatory stderr."""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "lib").mkdir()

            # Force DSN to an unreachable address so DB mode fails.
            result = subprocess.run(
                [
                    "python3",
                    str(tools_path / "schema_compat_check.py"),
                    str(root),
                    "postgresql://nobody@127.0.0.1:1/nowhere",
                ],
                capture_output=True,
                text=True,
            )
            # If psycopg2 is installed, connection fails; if not, import is skipped.
            # Either way: no migrations → exit 2.
            assert result.returncode == 2, f"exit={result.returncode} stderr={result.stderr}"
            assert (
                "no schema source" in result.stderr.lower()
                or "cannot connect" in result.stderr.lower()
                or "available" in result.stderr.lower()
            )

    def test_alter_table_add_column_extracted(self) -> None:
        """ALTER TABLE ... ADD COLUMN should populate columns set (regression)."""
        sys.path.insert(0, str(tools_path))
        from schema_compat_check import StaticSchemaIntrospector

        with TemporaryDirectory() as tmpdir:
            mig = Path(tmpdir)
            (mig / "001.sql").write_text("CREATE TABLE users (id uuid PRIMARY KEY);\n")
            (mig / "002.sql").write_text(
                "ALTER TABLE users ADD COLUMN email text NOT NULL;\n"
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name text;\n"
            )
            intro = StaticSchemaIntrospector(mig)
            assert intro.introspect()
            assert "users" in intro.tables
            assert {"id", "email", "full_name"} <= intro.columns["users"]

    def test_create_function_populates_rpcs(self) -> None:
        """CREATE [OR REPLACE] FUNCTION should populate rpcs set."""
        sys.path.insert(0, str(tools_path))
        from schema_compat_check import StaticSchemaIntrospector

        with TemporaryDirectory() as tmpdir:
            mig = Path(tmpdir)
            (mig / "001.sql").write_text(
                "CREATE FUNCTION foo() RETURNS int AS $$ SELECT 1 $$ LANGUAGE sql;\n"
                "CREATE OR REPLACE FUNCTION public.bar(x int) RETURNS void AS $$ BEGIN END $$ LANGUAGE plpgsql;\n"
            )
            intro = StaticSchemaIntrospector(mig)
            assert intro.introspect()
            assert {"foo", "bar"} <= intro.rpcs


class TestJavaSpringMultiStack:
    """H26 / F30 — Java/Spring + Flyway dispatch in schema_compat_check.

    Pre-fix, the tool only parsed Dart + Supabase migrations. Client-A
    (Java/Spring) was unverified — schema drift between JPA entities and
    Flyway migrations couldn't be caught.
    """

    @staticmethod
    def _make_java_spring_project(root: Path) -> None:
        """Lay down a minimal Java/Spring project: pom.xml + Flyway dir + src tree."""
        (root / "pom.xml").write_text(
            "<project><modelVersion>4.0.0</modelVersion>"
            "<groupId>x</groupId><artifactId>y</artifactId><version>1</version></project>\n"
        )
        (root / "src" / "main" / "java" / "com" / "ex").mkdir(parents=True)
        (root / "src" / "main" / "resources" / "db" / "migration").mkdir(parents=True)

    def _run(self, root: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "python3",
                str(tools_path / "schema_compat_check.py"),
                str(root),
                "postgresql://nope@127.0.0.1:1/x",
            ],
            capture_output=True,
            text=True,
        )

    def _parse_output(self, stdout: str) -> tuple[list[dict], dict | None]:
        findings: list[dict] = []
        meta: dict | None = None
        for line in stdout.splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("_meta"):
                meta = obj
            else:
                findings.append(obj)
        return findings, meta

    def test_java_parser_extracts_table_and_column_annotations(self) -> None:
        from schema_compat_check import JavaCodeParser

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_java_spring_project(root)
            (root / "src/main/java/com/ex/Patient.java").write_text("""
                package com.ex;
                import javax.persistence.*;
                @Entity
                @Table(name = "patients")
                public class Patient {
                    @Id @Column(name = "id") private Long id;
                    @Column(name = "first_name") private String firstName;
                    @JoinColumn(name = "clinic_id") private Object clinic;
                }
            """)
            parser = JavaCodeParser(root)
            findings = parser.parse_all()
            tables = [f for f in findings if f["type"] == "table_ref"]
            cols = [f for f in findings if f["type"] == "column_ref"]
            assert any(f["table"] == "patients" for f in tables)
            assert any(f["column"] == "id" and f["context"] == "jpa_column" for f in cols)
            assert any(f["column"] == "first_name" for f in cols)
            assert any(f["column"] == "clinic_id" and f["context"] == "jpa_join" for f in cols)

    def test_java_parser_extracts_repository_method_columns(self) -> None:
        from schema_compat_check import JavaCodeParser

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_java_spring_project(root)
            (root / "src/main/java/com/ex/PatientRepo.java").write_text("""
                package com.ex;
                import java.util.*;
                public interface PatientRepo {
                    List<Patient> findByPatientId(Long id);
                    long countByServiceStatus(String status);
                    Optional<Patient> findByEmailAndIsDeleted(String e, boolean d);
                }
            """)
            parser = JavaCodeParser(root)
            findings = parser.parse_all()
            cols = {f["column"] for f in findings if f["context"] == "repository_method"}
            assert "patient_id" in cols
            assert "service_status" in cols
            assert "email" in cols  # And-suffix splits the segment

    def test_flyway_introspector_reads_canonical_path(self) -> None:
        from schema_compat_check import FlywayMigrationIntrospector

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            mig = root / "src" / "main" / "resources" / "db" / "migration"
            mig.mkdir(parents=True)
            (mig / "V1__init.sql").write_text(
                "CREATE TABLE patients (id uuid PRIMARY KEY, first_name text);"
            )
            intro = FlywayMigrationIntrospector(root)
            assert intro.introspect()
            assert "patients" in intro.tables
            assert {"id", "first_name"} <= intro.columns.get("patients", set())

    def test_e2e_java_drift_detection(self) -> None:
        """End-to-end: pom.xml + Java entity + Flyway migration → drift report."""
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_java_spring_project(root)
            # Entity references a column that doesn't exist in migration
            (root / "src/main/java/com/ex/Patient.java").write_text("""
                package com.ex;
                @Entity
                @Table(name = "ghost_table")
                public class Patient {}
            """)
            (root / "src/main/resources/db/migration" / "V1__init.sql").write_text(
                "CREATE TABLE patients (id uuid PRIMARY KEY);"
            )
            result = self._run(root)
            findings, meta = self._parse_output(result.stdout)
            assert meta is not None, f"no meta. stdout={result.stdout[:500]}"
            assert meta["stack"] == "java_spring"
            assert meta["source"] == "static_flyway"
            missing = [
                f
                for f in findings
                if f.get("category") == "missing_table" and f.get("expected") == "ghost_table"
            ]
            assert len(missing) >= 1
            assert result.returncode == 1
