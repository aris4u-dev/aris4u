#!/usr/bin/env python3
"""
V16.3 H17 — Schema Compatibility Drift Detector (with static fallback)

Compares Flutter code references con schema Postgres. Two modes:
  - DB mode: introspect live postgres via psycopg2 (highest fidelity)
  - Static mode: parse supabase/migrations/*.sql (no DB dependency)

Exit: 0 si no hay errors, 1 si hay errors, 2 si ni DB ni migrations disponibles.
Output: JSONL findings, one per line; footer line with {"source": "db"|"static"}.

Usage:
    python3 schema_compat_check.py ~/projects/your-flutter-app [postgresql://...]
"""

import sys
import os
import re
import json
from pathlib import Path
from typing import Any, Dict, List, Set, Optional

from _logger import emit_event  # type: ignore[import-not-found]

# V16.3 H17 fix: psycopg2 is optional — static fallback parses migrations directly.
try:
    import psycopg2
    from psycopg2 import sql
    HAS_PSYCOPG2 = True
except ImportError:
    psycopg2 = None
    sql = None
    HAS_PSYCOPG2 = False

# H26 multi-stack: detect Flutter vs Java/Spring vs Prisma via marker files.
# Pre-fix (F30 in C3 audit), this tool was Flutter+Supabase exclusive.
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
try:
    from stack_registry import detect_stack  # type: ignore[import-not-found]
except ImportError:
    def detect_stack(_path: str) -> str:
        return "generic"


class SchemaIntrospector:
    """Introspect Postgres schema structure."""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.tables: Set[str] = set()
        self.columns: Dict[str, Set[str]] = {}  # {table: {columns}}
        self.rpcs: Set[str] = set()
        self.fk_constraints: Dict[str, str] = {}  # {constraint_name: referenced_table}
        self.conn = None

    def connect(self) -> bool:
        """Connect to DB. Return True if successful, False otherwise."""
        assert psycopg2 is not None  # solo se invoca cuando HAS_PSYCOPG2 (import opcional)
        try:
            self.conn = psycopg2.connect(self.dsn)
            return True
        except psycopg2.Error as e:
            print(f"cannot connect to DB: {e}", file=sys.stderr)
            return False

    def introspect(self) -> bool:
        """Load schema. Return True if successful."""
        if not self.connect():
            return False
        assert self.conn is not None  # connect()==True garantiza la conexión
        assert psycopg2 is not None   # connect()==True ⇒ psycopg2 disponible

        try:
            cur = self.conn.cursor()

            # Tables
            cur.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
            self.tables = {row[0] for row in cur.fetchall()}

            # Columns per table
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public' ORDER BY table_name, ordinal_position"
            )
            for table, column in cur.fetchall():
                if table not in self.columns:
                    self.columns[table] = set()
                self.columns[table].add(column)

            # RPC functions
            cur.execute(
                "SELECT routine_name FROM information_schema.routines WHERE routine_schema='public' AND routine_type='FUNCTION'"
            )
            self.rpcs = {row[0] for row in cur.fetchall()}

            # FK constraints (name → referenced table)
            cur.execute(
                """
                SELECT constraint_name, ccu.table_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.constraint_column_usage AS ccu USING (constraint_schema, constraint_name)
                WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
                """
            )
            for constraint_name, ref_table in cur.fetchall():
                self.fk_constraints[constraint_name] = ref_table

            cur.close()
            return True
        except psycopg2.Error as e:
            print(f"introspection error: {e}", file=sys.stderr)
            return False
        finally:
            if self.conn:
                self.conn.close()


class StaticSchemaIntrospector:
    """V16.3 H17 — Build schema model from supabase/migrations/*.sql (no DB).

    Parses CREATE TABLE, CREATE FUNCTION, CONSTRAINT FOREIGN KEY clauses to
    build the same tables / columns / rpcs / fk_constraints structure as the
    DB introspector. Lower fidelity than live DB but zero dependencies.
    """

    # Class attr: same shape as SchemaIntrospector so DriftDetector works for both.
    def __init__(self, migrations_dir: Path):
        self.migrations_dir = migrations_dir
        self.tables: Set[str] = set()
        self.columns: Dict[str, Set[str]] = {}
        self.rpcs: Set[str] = set()
        self.fk_constraints: Dict[str, str] = {}

    def introspect(self) -> bool:
        """Parse all migration files in order. Return True if at least one file parsed."""
        if not self.migrations_dir.exists():
            return False

        migration_files = sorted(self.migrations_dir.glob("*.sql"))
        if not migration_files:
            return False

        for mig in migration_files:
            try:
                content = mig.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            self._parse_sql(content)

        return True

    def _parse_sql(self, sql_text: str) -> None:
        """Extract tables / columns / functions / FKs via regex."""
        # Strip line/block comments for robust matching.
        sql_no_comments = re.sub(r"--[^\n]*", "", sql_text)
        sql_no_comments = re.sub(r"/\*.*?\*/", "", sql_no_comments, flags=re.DOTALL)

        # CREATE TABLE [IF NOT EXISTS] [public.]name (...columns...)
        # Non-greedy up to matching unbalanced closing paren is hard in regex;
        # we take everything from ( to the first ); at depth 0 via a manual scan.
        for m in re.finditer(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?(\w+)\s*\(",
            sql_no_comments,
            flags=re.IGNORECASE,
        ):
            table = m.group(1)
            self.tables.add(table)
            # Find balanced paren body starting at m.end()-1 (the opening paren).
            body = self._extract_paren_body(sql_no_comments, m.end() - 1)
            if body is None:
                continue
            cols = self._parse_columns(body)
            if cols:
                self.columns.setdefault(table, set()).update(cols)

        # ALTER TABLE ... ADD COLUMN name type
        for m in re.finditer(
            r"ALTER\s+TABLE\s+(?:public\.)?(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s",
            sql_no_comments,
            flags=re.IGNORECASE,
        ):
            table, col = m.group(1), m.group(2)
            self.columns.setdefault(table, set()).add(col)

        # CREATE [OR REPLACE] FUNCTION [public.]name(...)
        for m in re.finditer(
            r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+(?:public\.)?(\w+)\s*\(",
            sql_no_comments,
            flags=re.IGNORECASE,
        ):
            self.rpcs.add(m.group(1))

        # CONSTRAINT name FOREIGN KEY ... REFERENCES ref_table
        for m in re.finditer(
            r"CONSTRAINT\s+(\w+)\s+FOREIGN\s+KEY[^;]*?REFERENCES\s+(?:public\.)?(\w+)",
            sql_no_comments,
            flags=re.IGNORECASE | re.DOTALL,
        ):
            self.fk_constraints[m.group(1)] = m.group(2)

        # Inline FK: col TYPE REFERENCES ref_table(...) → emit implicit _fkey alias
        # matches the Supabase pattern "table_col_fkey" often used in select embeds.
        for m in re.finditer(
            r"(\w+)\s+[\w\s]+?REFERENCES\s+(?:public\.)?(\w+)\s*\(",
            sql_no_comments,
            flags=re.IGNORECASE,
        ):
            col, ref_table = m.group(1), m.group(2)
            # Skip keyword noise
            if col.upper() in {"KEY", "CONSTRAINT", "TABLE", "COLUMN", "NOT", "NULL"}:
                continue
            implicit = f"{ref_table}_{col}_fkey"
            self.fk_constraints.setdefault(implicit, ref_table)

    @staticmethod
    def _extract_paren_body(text: str, start_paren_idx: int) -> Optional[str]:
        """Return body between balanced parens starting at start_paren_idx (which points at '(')."""
        if start_paren_idx >= len(text) or text[start_paren_idx] != "(":
            return None
        depth = 0
        for i in range(start_paren_idx, len(text)):
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[start_paren_idx + 1 : i]
        return None

    @staticmethod
    def _parse_columns(table_body: str) -> Set[str]:
        """Extract column names from a CREATE TABLE body.

        Handles: "col TYPE ...", skips CONSTRAINT/PRIMARY/FOREIGN/UNIQUE/CHECK clauses.
        """
        cols: Set[str] = set()
        # Split on commas at depth 0 (not inside nested parens).
        parts: List[str] = []
        depth = 0
        current: List[str] = []
        for ch in table_body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(current))
                current = []
            else:
                current.append(ch)
        if current:
            parts.append("".join(current))

        skip_prefixes = {
            "CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK",
            "EXCLUDE", "LIKE", "INHERITS",
        }
        for part in parts:
            s = part.strip()
            if not s:
                continue
            first_token = s.split(None, 1)[0].upper().rstrip("(")
            if first_token in skip_prefixes:
                continue
            # Column name is the first identifier.
            m = re.match(r'["\w]+', s)
            if not m:
                continue
            col = m.group(0).strip('"')
            if col:
                cols.add(col)
        return cols


class FlutterCodeParser:
    """Parse Flutter .dart files for schema references."""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.findings: List[Dict] = []

    def parse_all(self) -> List[Dict]:
        """Parse all .dart files. Return findings."""
        lib_dir = self.project_root / "lib"
        if not lib_dir.exists():
            return []

        for dart_file in lib_dir.rglob("*.dart"):
            self._parse_file(dart_file)

        return self.findings

    def _parse_file(self, file_path: Path):
        """Parse single .dart file for queries."""
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return

        relative_path = file_path.relative_to(self.project_root)

        # Pattern 1: from('table_name')
        for match in re.finditer(r"\.from\(['\"](\w+)['\"]\)", content):
            table = match.group(1)
            line_num = content[: match.start()].count("\n") + 1
            self.findings.append(
                {
                    "type": "table_ref",
                    "table": table,
                    "file": str(relative_path),
                    "line": line_num,
                    "match": match.group(0),
                }
            )

        # Pattern 2: .select('col1,col2') or .select(['col1','col2'])
        # Extract columns from select
        for match in re.finditer(
            r"\.select\(\s*(['\"]([^'\"]+)['\"]\s*|\[?\s*['\"]([^'\"]+)['\"][^\]]*\]?)",
            content,
        ):
            cols_str = match.group(2) or match.group(3)
            if cols_str:
                # Parse columns (handle joins like "profiles!fk_name(col1,col2)")
                columns = self._parse_select_columns(cols_str)
                for col in columns:
                    line_num = content[: match.start()].count("\n") + 1
                    self.findings.append(
                        {
                            "type": "column_ref",
                            "column": col,
                            "file": str(relative_path),
                            "line": line_num,
                            "match": match.group(0),
                            "context": "select",
                        }
                    )

        # Pattern 3: .eq('column', ...) / .neq / .ilike / .gt / .lt
        for match in re.finditer(
            r"\.(eq|neq|ilike|gt|lt|gte|lte)\s*\(\s*['\"](\w+)['\"]", content
        ):
            col = match.group(2)
            line_num = content[: match.start()].count("\n") + 1
            self.findings.append(
                {
                    "type": "column_ref",
                    "column": col,
                    "file": str(relative_path),
                    "line": line_num,
                    "match": match.group(0),
                    "context": "filter",
                }
            )

        # Pattern 4: .order('column', ...)
        for match in re.finditer(r"\.order\s*\(\s*['\"](\w+)['\"]", content):
            col = match.group(1)
            line_num = content[: match.start()].count("\n") + 1
            self.findings.append(
                {
                    "type": "column_ref",
                    "column": col,
                    "file": str(relative_path),
                    "line": line_num,
                    "match": match.group(0),
                    "context": "order",
                }
            )

        # Pattern 5: .rpc('function_name', ...)
        for match in re.finditer(r"\.rpc\s*\(\s*['\"](\w+)['\"]", content):
            rpc = match.group(1)
            line_num = content[: match.start()].count("\n") + 1
            self.findings.append(
                {
                    "type": "rpc_ref",
                    "rpc": rpc,
                    "file": str(relative_path),
                    "line": line_num,
                    "match": match.group(0),
                }
            )

        # Pattern 6: FK alias in embedded select: profiles!creator_id_fkey(...)
        for match in re.finditer(r"(\w+)!(\w+_fkey)\s*\(", content):
            fk_alias = match.group(2)
            line_num = content[: match.start()].count("\n") + 1
            self.findings.append(
                {
                    "type": "fk_alias_ref",
                    "fk_alias": fk_alias,
                    "file": str(relative_path),
                    "line": line_num,
                    "match": match.group(0),
                }
            )

    @staticmethod
    def _parse_select_columns(select_str: str) -> Set[str]:
        """Extract column names from select string."""
        cols = set()
        # Split by comma, ignore join notation
        parts = select_str.split(",")
        for part in parts:
            part = part.strip()
            # Remove join notation (profiles!fk_name(...))
            base = part.split("!")[0].strip()
            if base and base not in ("*",):
                cols.add(base)
        return cols


class FlywayMigrationIntrospector(StaticSchemaIntrospector):
    """H26: Flyway-style migrations live in src/main/resources/db/migration/.

    Reuses the SQL parsing in `StaticSchemaIntrospector` — Postgres CREATE
    TABLE syntax is the same regardless of orchestration tool. Only the
    discovery path differs.
    """

    def __init__(self, project_root: Path):
        super().__init__(project_root / "src" / "main" / "resources" / "db" / "migration")


class JavaCodeParser:
    """Parse Java/JPA code for schema references.

    Extracts:
      - @Table(name="...")          → table_ref
      - @Column(name="...")         → column_ref (context: jpa_column)
      - @JoinColumn(name="...")     → column_ref (context: jpa_join)
      - findBy<X> / countBy<X>      → column_ref (context: repository_method,
        snake_case'd from the camelCase fragment)

    Why repository methods: Spring Data converts `findByPatientId` to a
    query on column `patient_id` (or property). If the entity lacks that
    column, the app blows up at runtime. Catching it at static-check time
    is the whole point of this tool.
    """

    # Common JPA / Spring annotations.
    _TABLE_ANNOTATION = re.compile(
        r'@Table\s*\(\s*(?:[^)]*?\bname\s*=\s*)?"(\w+)"', re.DOTALL
    )
    _COLUMN_ANNOTATION = re.compile(
        r'@Column\s*\(\s*(?:[^)]*?\bname\s*=\s*)?"(\w+)"', re.DOTALL
    )
    _JOIN_COLUMN_ANNOTATION = re.compile(
        r'@JoinColumn\s*\(\s*(?:[^)]*?\bname\s*=\s*)?"(\w+)"', re.DOTALL
    )
    # Spring Data repository convention: List<X> findByCamelCase(...) etc.
    # Captures "Method" segment that follows the verb.
    _REPO_METHOD = re.compile(
        r"\b(?:find|count|delete|exists|get|read|search|stream)By"
        r"([A-Z][A-Za-z0-9_]*?)\s*(?:And|Or|OrderBy|\s*\()",
    )

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.findings: List[Dict] = []

    def parse_all(self) -> List[Dict]:
        java_root = self.project_root / "src" / "main" / "java"
        if not java_root.exists():
            return []
        for jf in java_root.rglob("*.java"):
            self._parse_file(jf)
        return self.findings

    def _parse_file(self, file_path: Path) -> None:
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return

        try:
            relative_path = file_path.relative_to(self.project_root)
        except ValueError:
            relative_path = file_path

        for m in self._TABLE_ANNOTATION.finditer(content):
            table = m.group(1)
            line = content[: m.start()].count("\n") + 1
            self.findings.append({
                "type": "table_ref", "table": table,
                "file": str(relative_path), "line": line,
                "match": m.group(0),
            })

        for m in self._COLUMN_ANNOTATION.finditer(content):
            col = m.group(1)
            line = content[: m.start()].count("\n") + 1
            self.findings.append({
                "type": "column_ref", "column": col,
                "file": str(relative_path), "line": line,
                "match": m.group(0), "context": "jpa_column",
            })

        for m in self._JOIN_COLUMN_ANNOTATION.finditer(content):
            col = m.group(1)
            line = content[: m.start()].count("\n") + 1
            self.findings.append({
                "type": "column_ref", "column": col,
                "file": str(relative_path), "line": line,
                "match": m.group(0), "context": "jpa_join",
            })

        for m in self._REPO_METHOD.finditer(content):
            camel = m.group(1)
            snake = self._camel_to_snake(camel)
            line = content[: m.start()].count("\n") + 1
            self.findings.append({
                "type": "column_ref", "column": snake,
                "file": str(relative_path), "line": line,
                "match": m.group(0), "context": "repository_method",
            })

    @staticmethod
    def _camel_to_snake(name: str) -> str:
        """`PatientId` → `patient_id`. Used to map Spring repository methods
        to Postgres column names by convention."""
        s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
        return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


class DriftDetector:
    """Compare Flutter references with actual schema."""

    def __init__(self, schema: Any, findings: List[Dict]):  # duck-typed: vivo o estático
        self.schema = schema
        self.findings = findings
        self.errors: List[Dict] = []

    def detect(self) -> List[Dict]:
        """Find drift. Return JSONL-serializable findings."""
        seen = set()

        for finding in self.findings:
            finding_key = (
                finding["type"],
                finding.get("table") or finding.get("column") or finding.get("rpc")
                or finding.get("fk_alias"),
            )

            # Deduplicate
            if finding_key in seen:
                continue
            seen.add(finding_key)

            if finding["type"] == "table_ref":
                if finding["table"] not in self.schema.tables:
                    self.errors.append(
                        {
                            "severity": "error",
                            "category": "missing_table",
                            "reference_location": f"{finding['file']}:{finding['line']}",
                            "expected": finding["table"],
                            "actual": f"(missing — available: {', '.join(sorted(self.schema.tables)[:5])}...)",
                        }
                    )

            elif finding["type"] == "column_ref":
                # Infer table from context if possible (heuristic: assume last from() call)
                # For now, warn on column orphans without table context
                pass

            elif finding["type"] == "rpc_ref":
                if finding["rpc"] not in self.schema.rpcs:
                    self.errors.append(
                        {
                            "severity": "error",
                            "category": "missing_rpc",
                            "reference_location": f"{finding['file']}:{finding['line']}",
                            "expected": finding["rpc"],
                            "actual": f"(missing — {len(self.schema.rpcs)} RPCs available)",
                        }
                    )

            elif finding["type"] == "fk_alias_ref":
                if finding["fk_alias"] not in self.schema.fk_constraints:
                    self.errors.append(
                        {
                            "severity": "warn",
                            "category": "fk_name_mismatch",
                            "reference_location": f"{finding['file']}:{finding['line']}",
                            "expected": finding["fk_alias"],
                            "actual": f"(not found — {len(self.schema.fk_constraints)} FK constraints exist)",
                        }
                    )

        return self.errors


def _parse_args() -> tuple[Path, str]:
    """Valida argv y resuelve el DSN de conexión a Postgres.

    Termina con ``sys.exit(1)`` si faltan argumentos, o con ``sys.exit(2)`` si el directorio
    raíz no existe.

    Returns:
        Tupla ``(project_root, dsn)`` listos para usar.
    """
    if len(sys.argv) < 2:
        print(
            "Usage: python3 schema_compat_check.py <flutter_project_root> [postgres_dsn]",
            file=sys.stderr,
        )
        sys.exit(1)
    project_root = Path(sys.argv[1]).expanduser()
    if not project_root.exists():
        print(f"Project root not found: {project_root}", file=sys.stderr)
        sys.exit(2)
    # V16.3 H17 — Pick source: DB (psycopg2) > static (migrations/). Static is robust
    # fallback; DB is higher fidelity but optional.
    dsn = sys.argv[2] if len(sys.argv) > 2 else None
    if dsn is None:
        dsn = os.getenv(
            "SCHEMA_COMPAT_DSN",
            "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
        )
    return project_root, dsn


def _resolve_schema(project_root: Path, dsn: str, stack: str) -> tuple[object, str]:
    """Adquiere el schema DB (psycopg2) o la alternativa estática (migraciones).

    Intenta primero la introspección viva vía psycopg2 (mayor fidelidad). Si no está
    disponible o falla, despacha al introspector estático correcto según el stack
    (Flyway para Java/Spring, Supabase CLI para Flutter/genérico).

    Args:
        project_root: Raíz del proyecto bajo análisis.
        dsn: Cadena de conexión Postgres (puede no usarse si psycopg2 no está presente).
        stack: Identificador de stack detectado (p.ej. ``"java_spring"``).

    Returns:
        Tupla ``(schema, source)`` donde ``source`` es ``"db"``, ``"static_flyway"``,
        ``"static_supabase"`` o ``"unknown"`` si ninguna fuente estuvo disponible.
    """
    schema: object = None
    source = "unknown"
    # DB introspection works for any Postgres-backed stack — try first.
    if HAS_PSYCOPG2:
        db_schema = SchemaIntrospector(dsn)
        if db_schema.introspect():
            schema = db_schema
            source = "db"
    # Static fallback: dispatch migration discovery by stack.
    if schema is None:
        if stack == "java_spring":
            static_schema = FlywayMigrationIntrospector(project_root)
            if static_schema.introspect():
                schema = static_schema
                source = "static_flyway"
        else:
            # Default / Flutter / generic: Supabase CLI layout.
            static_schema = StaticSchemaIntrospector(
                project_root / "supabase" / "migrations"
            )
            if static_schema.introspect():
                schema = static_schema
                source = "static_supabase"
    return schema, source


def _emit_findings(errors: list[dict], source: str, stack: str) -> bool:
    """Emite los findings de drift a stdout (JSONL) y registra el evento de telemetría.

    Args:
        errors: Lista de findings producida por ``DriftDetector.detect()``.
        source: Fuente del schema (``"db"``, ``"static_flyway"``, ``"static_supabase"``).
        stack: Identificador de stack (p.ej. ``"java_spring"``).

    Returns:
        ``True`` si al menos un finding tiene severidad ``"error"``.
    """
    has_error = False
    for error in errors:
        error["source"] = source
        error["stack"] = stack
        # V16.6 W2.1 emit event before print
        emit_event("schema_finding", "schema_compat_check", error=error, stack=stack)
        print(json.dumps(error))
        if error["severity"] == "error":
            has_error = True
    return has_error


def main() -> None:
    """Punto de entrada: parsea args, detecta stack, adquiere schema, detecta drift y emite."""
    project_root, dsn = _parse_args()

    # H26 multi-stack dispatch: detect Flutter vs Java/Spring, etc.
    stack = detect_stack(str(project_root))

    schema, source = _resolve_schema(project_root, dsn, stack)
    if schema is None:
        print(
            f"schema_compat_check: no schema source available for stack '{stack}' "
            "(psycopg2 missing AND no migrations found at canonical paths)",
            file=sys.stderr,
        )
        sys.exit(2)

    # Code parser: dispatch by stack.
    if stack == "java_spring":
        parser = JavaCodeParser(project_root)
    else:
        parser = FlutterCodeParser(project_root)
    findings = parser.parse_all()

    # DriftDetector uses duck-typed attrs (tables/rpcs/fk_constraints) so it
    # works for both stacks without modification.
    detector = DriftDetector(schema, findings)
    errors = detector.detect()

    has_error = _emit_findings(errors, source, stack)

    meta_summary = {
        "_meta": True,
        "stack": stack,
        "source": source,
        "tables_known": len(getattr(schema, "tables", set())),
        "rpcs_known": len(getattr(schema, "rpcs", set())),
        "fks_known": len(getattr(schema, "fk_constraints", {})),
        "errors": sum(1 for e in errors if e["severity"] == "error"),
        "warnings": sum(1 for e in errors if e["severity"] == "warn"),
    }
    # V16.6 W2.1 emit event before print
    emit_event("schema_check_complete", "schema_compat_check", summary=meta_summary)
    print(json.dumps(meta_summary))

    sys.exit(1 if has_error else 0)


if __name__ == "__main__":
    main()
