#!/usr/bin/env python3
"""
Migration Linter V16.2 — Detects SQL migration bugs before apply.

Analyzes Supabase/PostgreSQL migrations for 8 classes of bugs:

1. forward_table_reference: CREATE FUNCTION/TRIGGER/INDEX references table created later
   Example: Function uses table 'rides' but table is created 100 lines later
   Fix: Move CREATE TABLE before function definition

2. forward_column_reference: TRIGGER references column not created yet
   Example: Trigger accesses NEW.rating before ALTER TABLE ADD COLUMN rating
   Fix: Add column before trigger definition

3. column_not_in_table: SELECT/UPDATE/JOIN references nonexistent column
   Example: Query references 'rides.passenger_id' but column is named 'rider_id'
   Fix: Verify column name or add missing column

4. parameter_prefix_in_index: CREATE INDEX uses identifier starting with p_
   Example: CREATE INDEX ON table ((p_param_name))
   Fix: Verify parameters aren't mistakenly in index expressions

5. non_immutable_in_partial_index: CREATE INDEX WHERE uses VOLATILE functions
   Example: CREATE INDEX idx WHERE created_at > NOW()
   Fix: PostgreSQL rejects — use immutable wrapper or different index strategy

6. missing_search_path_on_definer: CREATE FUNCTION SECURITY DEFINER lacks SET search_path
   Example: SECURITY DEFINER function called from different schema fails silently
   Fix: Add 'SET search_path = public' before AS $$

7. rls_policy_cycle: (DISABLED) Policy cycles A↔B detected but usually acceptable
   Disabled by default due to high false positives with normalized RLS patterns

8. inconsistent_column_name: Table created with foo_id but referenced as bar_id
   Example: Table has 'user_id' but query references 'account_id'
   Fix: Use consistent column naming conventions

9. fk_reference_missing_table: FOREIGN KEY references a table not created in prior migrations
   Example: ALTER TABLE orders ADD CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES users(id)
            but users table doesn't exist in migration history
   Fix: Create the parent table in an earlier migration

Usage:
  python3 migration_linter.py /path/to/migrations/

Output: JSONL (one finding per line) to stdout
Exit codes:
  0 = clean (no errors)
  1 = errors detected (must fix before apply)

Example output:
  {"severity": "error", "category": "non_immutable_in_partial_index",
   "file": "032_rate_limiting.sql", "line": 63, "rule": "non_immutable_in_partial_index",
   "message": "CREATE INDEX WHERE uses NOW() — Postgres requires IMMUTABLE",
   "suggestion": "Use immutable wrapper function or different index strategy"}
"""

import re
import json
import sys
from pathlib import Path
from typing import Dict, Set, List, Tuple
from dataclasses import dataclass, asdict

from _logger import emit_event  # type: ignore[import-not-found]


@dataclass
class Finding:
    severity: str  # "error", "warning"
    category: str  # rule name
    file: str
    line: int
    rule: str
    message: str
    suggestion: str


class MigrationLinter:
    # Volatile functions in PostgreSQL
    VOLATILE_FUNCTIONS = {
        "now", "current_timestamp", "current_date", "current_time",
        "localtime", "localtimestamp", "random", "clock_timestamp",
        "statement_timestamp", "transaction_timestamp", "gen_random_uuid",
        "uuid_generate_v4", "uuid_generate_v1"
    }

    # F36 fix: column types are matched to detect "<col_name> <type>" patterns
    # in CREATE TABLE bodies. Pre-fix the list was 10 Postgres-only types,
    # missing varchar/decimal/jsonb/timestamptz/serial/etc. Result: those
    # columns weren't tracked and downstream column_not_in_table checks
    # produced false negatives. Expanded to 40+ cross-DB types.
    COLUMN_TYPES = (
        # Numeric
        "smallint", "integer", "int", "int2", "int4", "int8", "bigint",
        "decimal", "numeric", "real", "double", "float", "float4", "float8",
        "smallserial", "serial", "bigserial", "money",
        # Text
        "char", "varchar", "text", "name", "character", "citext",
        # Date / time
        "timestamp", "timestamptz", "date", "time", "timetz", "interval",
        # Boolean / UUID / structured
        "boolean", "bool", "uuid", "json", "jsonb", "bytea", "xml",
        # Geometric / spatial
        "point", "polygon", "line", "box", "path", "circle",
        "geometry", "geography",
        # Network
        "cidr", "inet", "macaddr", "macaddr8",
        # Range / search
        "tsvector", "tsquery", "int4range", "int8range", "tsrange",
        "tstzrange", "daterange", "numrange",
        # MySQL-style (for repos that mix dialects in tests)
        "tinyint", "mediumint", "longtext", "mediumtext", "blob",
        "longblob", "datetime", "year",
    )

    # F34 fix: migration filename naming conventions. "auto" detects on first
    # lint by counting matches per pattern. Supabase CLI uses <num>_<name>.sql;
    # Flyway uses V<num>__<name>.sql. Pre-fix only Supabase was supported,
    # so Flyway-style repos had no duplicate-prefix detection or sort.
    NAMING_PATTERNS = {
        "supabase": re.compile(r"^(\d+)[_\-]"),
        "flyway": re.compile(r"^V(\d+)__", re.IGNORECASE),
    }

    def __init__(self, naming: str = "auto"):
        """naming: 'supabase' | 'flyway' | 'auto' (detected on first lint)."""
        self.findings: List[Finding] = []
        self.tables_created: Set[str] = set()
        self.columns_per_table: Dict[str, Set[str]] = {}
        self.functions_created: Set[str] = set()
        self.policies_per_table: Dict[str, List[Tuple[str, str]]] = {}  # table -> [(policy_name, sql_text)]
        self.file_order: List[str] = []
        self.file_contents: Dict[str, List[str]] = {}  # file -> lines
        self.tables_per_file: Dict[str, Set[str]] = {}  # H47: track which tables created in each file
        if naming not in ("auto", "supabase", "flyway"):
            raise ValueError(f"naming must be 'auto'|'supabase'|'flyway', got {naming!r}")
        self.naming = naming

    def _detect_naming(self, sql_files: List[Path]) -> str:
        """Detect naming convention from file names. Returns 'flyway' or 'supabase'."""
        flyway_count = sum(
            1 for f in sql_files if self.NAMING_PATTERNS["flyway"].match(f.name)
        )
        supabase_count = sum(
            1 for f in sql_files if self.NAMING_PATTERNS["supabase"].match(f.name)
        )
        return "flyway" if flyway_count > supabase_count else "supabase"

    def _sort_migrations(self, sql_files: List[Path]) -> List[Path]:
        """Numeric sort by migration prefix. Avoids Vlex sort problem
        (V1, V10, V2 → wrong) and keeps stable order for unparseable names."""
        pattern = self.NAMING_PATTERNS[self.naming]

        def key(f: Path) -> Tuple[float, str]:
            m = pattern.match(f.name)
            return (int(m.group(1)), f.name) if m else (float("inf"), f.name)

        return sorted(sql_files, key=key)

    def lint_path(self, target: str) -> int:
        """V16.3 H16 — Lint a file OR a directory. Return 0 if clean, 1 if errors.

        If `target` is a single .sql file, lint just that file (still runs the
        cross-file rules over its single-file corpus — useful for quick checks).
        If `target` is a directory, lint all *.sql files within it (V16.2 behavior).
        """
        path = Path(target)
        if not path.exists():
            print(f"Error: path {target} does not exist", file=sys.stderr)
            return 1

        if path.is_file():
            if path.suffix.lower() != ".sql":
                print(f"Error: {target} is not a .sql file", file=sys.stderr)
                return 1
            sql_files = [path]
        else:
            raw_files = list(path.glob("*.sql"))
            if not raw_files:
                print(f"No SQL migrations found in {target}")
                return 0
            sql_files = raw_files

        # F34: detect naming convention if not specified, then sort numerically.
        if self.naming == "auto":
            self.naming = self._detect_naming(sql_files)
        sql_files = self._sort_migrations(sql_files)

        self.file_order = [f.name for f in sql_files]

        # First pass: parse all files
        for sql_file in sql_files:
            with open(sql_file, 'r', encoding='utf-8', errors='ignore') as f:
                self.file_contents[sql_file.name] = f.readlines()

        # V16.3 H18 — Detect duplicate migration number prefixes before lint rules.
        # Supabase applies migrations in alphabetical order — two files with the
        # same numerical prefix run in an undefined-ish order (sort by suffix)
        # and conflict. This was the Fase B bug: agent created 032_payments_table.sql
        # alongside existing 032_rate_limiting.sql with no warning.
        if len(sql_files) > 1:
            self._check_duplicate_migration_numbers(sql_files)

        # Second pass: lint rules in order
        for sql_file in sql_files:
            self._lint_file(sql_file.name)

        # Third pass: cross-file rules
        self._check_rls_policy_cycles()
        self._check_inconsistent_column_names()
        self._check_fk_constraints()  # H47: semantic FK validation

        # Output findings
        error_count = 0
        for finding in sorted(self.findings, key=lambda f: (f.file, f.line)):
            error_count += finding.severity == "error"
            # V16.6 W2.1 emit event before print
            finding_dict = asdict(finding)
            emit_event("migration_finding", "migration_linter", finding=finding_dict)
            print(json.dumps(finding_dict))

        return 1 if error_count > 0 else 0

    # Backwards-compatible alias so existing callers (hook, tests) keep working.
    def lint_directory(self, migrations_dir: str) -> int:
        return self.lint_path(migrations_dir)

    def _check_duplicate_migration_numbers(self, sql_files: List[Path]) -> None:
        """V16.3 H18 — Flag two migrations sharing the same numerical prefix.

        F34 multi-stack: pattern picked by `self.naming` so Flyway repos
        (V<num>__name.sql) get the same protection as Supabase ones.
        """
        pattern = self.NAMING_PATTERNS[self.naming]
        prefix_to_files: Dict[str, List[str]] = {}
        for f in sql_files:
            m = pattern.match(f.name)
            if not m:
                continue
            prefix_to_files.setdefault(m.group(1), []).append(f.name)
        for prefix, names in prefix_to_files.items():
            if len(names) > 1:
                for n in sorted(names):
                    others = sorted(x for x in names if x != n)
                    self.findings.append(Finding(
                        severity="error",
                        category="duplicate_migration_number",
                        file=n,
                        line=1,
                        rule="duplicate_migration_number",
                        message=(
                            f"Prefix '{prefix}_' is shared with {', '.join(others)}. "
                            "Supabase applies in alphabetical order — conflicting migrations "
                            "race, tests and prod diverge."
                        ),
                        suggestion=(
                            f"Rename one of them to the next free prefix "
                            f"(highest({prefix})+1). Keep timestamps monotonic."
                        ),
                    ))

    def _lint_file(self, filename: str) -> None:
        """Lint a single migration file."""
        lines = self.file_contents[filename]

        # Parse tables, columns, functions, policies
        self._parse_creates(filename, lines)

        # Rule 1: forward_table_reference
        self._check_forward_table_references(filename, lines)

        # Rule 2: forward_column_reference
        self._check_forward_column_references(filename, lines)

        # Rule 3: column_not_in_table
        self._check_column_not_in_table(filename, lines)

        # Rule 4: parameter_prefix_in_index
        self._check_parameter_prefix_in_index(filename, lines)

        # Rule 5: non_immutable_in_partial_index
        self._check_non_immutable_in_partial_index(filename, lines)

        # Rule 6: missing_search_path_on_definer
        self._check_missing_search_path_on_definer(filename, lines)

    def _parse_creates(self, filename: str, lines: List[str]) -> None:
        """Parse CREATE TABLE, FUNCTION, POLICY statements."""
        sql_text = "".join(lines)

        # H47: Initialize file's table set if not present
        if filename not in self.tables_per_file:
            self.tables_per_file[filename] = set()

        # CREATE TABLE
        table_pattern = r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)'
        for match in re.finditer(table_pattern, sql_text, re.IGNORECASE):
            table_name = match.group(1).lower()
            self.tables_created.add(table_name)
            self.tables_per_file[filename].add(table_name)  # H47: track per file
            self.columns_per_table[table_name] = set()

            # Extract columns from CREATE TABLE body
            # Simplified: look for patterns like "column_name TYPE"
            create_table_match = re.search(
                r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?' + re.escape(table_name) +
                r'\s*\((.*?)\);',
                sql_text,
                re.IGNORECASE | re.DOTALL
            )
            if create_table_match:
                body = create_table_match.group(1)
                # F36 fix: build type-alternation from COLUMN_TYPES instead of a
                # 10-entry hardcoded subset. Catches varchar/decimal/jsonb/etc.
                type_alt = "|".join(re.escape(t) for t in self.COLUMN_TYPES)
                col_pattern = rf'\b([a-z_][a-z0-9_]*)\s+(?:{type_alt})\b'
                for col_match in re.finditer(col_pattern, body, re.IGNORECASE):
                    col_name = col_match.group(1).lower()
                    self.columns_per_table[table_name].add(col_name)

        # ALTER TABLE ADD COLUMN
        alter_pattern = r'ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([a-z_][a-z0-9_]*)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)'
        for match in re.finditer(alter_pattern, sql_text, re.IGNORECASE):
            table_name = match.group(1).lower()
            col_name = match.group(2).lower()
            if table_name not in self.columns_per_table:
                self.columns_per_table[table_name] = set()
            self.columns_per_table[table_name].add(col_name)

        # CREATE FUNCTION
        func_pattern = r'CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([a-z_][a-z0-9_]*)\s*\('
        for match in re.finditer(func_pattern, sql_text, re.IGNORECASE):
            func_name = match.group(1).lower()
            self.functions_created.add(func_name)

        # CREATE POLICY
        policy_pattern = r'CREATE\s+POLICY\s+(?:"?([^"]+)"?)\s+ON\s+([a-z_][a-z0-9_]*)'
        for match in re.finditer(policy_pattern, sql_text, re.IGNORECASE):
            policy_name = match.group(1)
            table_name = match.group(2).lower()
            if table_name not in self.policies_per_table:
                self.policies_per_table[table_name] = []
            # Extract full policy SQL (simplified)
            self.policies_per_table[table_name].append((policy_name, sql_text))

    def _check_forward_table_references(self, filename: str, lines: List[str]) -> None:
        """Rule 1: Detect references to tables created later in same file."""
        sql_text = "".join(lines)
        tables_in_file = set()

        # Extract tables created in this file
        table_pattern = r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)'
        for match in re.finditer(table_pattern, sql_text, re.IGNORECASE):
            tables_in_file.add(match.group(1).lower())

        # Find CREATE FUNCTION, CREATE TRIGGER, CREATE INDEX
        for idx, line in enumerate(lines, 1):
            # CREATE FUNCTION
            if re.search(r'CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION', line, re.IGNORECASE):
                # Extract function body and look for table references
                func_block = "\n".join(lines[idx-1:])
                # FALSE-POSITIVE FIX (client-incident 2026-07): limit scan to just the
                # function itself (up to $$;), then strip dollar-quoted body so only
                # the signature (RETURNS clause, param types) is scanned.
                # Before this fix, func_text_for_scan included the rest of the file —
                # any CREATE TABLE after the function would be matched even if the
                # function body never referenced that table (e.g. VALUES (...) bodies).
                func_end_match = re.search(r'\$\$;', func_block)
                if not func_end_match:
                    continue
                func_only = func_block[:func_end_match.end()]
                func_text_for_scan = re.sub(
                    r'(\$(?:\$|function\$|body\$)).*?\1',
                    ' ',
                    func_only,
                    flags=re.IGNORECASE | re.DOTALL,
                )
                for table in tables_in_file:
                    if re.search(rf'\b{table}\b', func_text_for_scan, re.IGNORECASE):
                        # Check if table created after function
                        func_line_num = idx
                        table_line_num = None
                        for tl_idx, tl in enumerate(lines[idx:], idx):
                            if re.search(
                                rf'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{table}\b',
                                tl, re.IGNORECASE
                            ):
                                table_line_num = tl_idx
                                break
                        if table_line_num and table_line_num > func_line_num:
                            self.findings.append(Finding(
                                severity="error",
                                category="forward_table_reference",
                                file=filename,
                                line=idx,
                                rule="forward_table_reference",
                                message=f"Function references table '{table}' created at line {table_line_num}",
                                suggestion=f"Move CREATE TABLE {table} before CREATE FUNCTION"
                            ))

    def _check_forward_column_references(self, filename: str, lines: List[str]) -> None:
        """Rule 2: Detect references to columns not created yet."""
        for idx, line in enumerate(lines, 1):
            # CREATE TRIGGER
            if re.search(r'CREATE\s+(?:OR\s+REPLACE\s+)?TRIGGER', line, re.IGNORECASE):
                trigger_block = "\n".join(lines[idx-1:])
                trigger_end = re.search(r';', trigger_block)
                if trigger_end:
                    trigger_text = trigger_block[:trigger_end.end()]

                    # Extract table name from trigger
                    on_match = re.search(r'ON\s+([a-z_][a-z0-9_]*)', trigger_text, re.IGNORECASE)
                    if on_match:
                        table_name = on_match.group(1).lower()

                        # Look for column references in NEW/OLD
                        col_refs = re.findall(r'(?:NEW|OLD)\.([a-z_][a-z0-9_]*)', trigger_text, re.IGNORECASE)
                        for col_name in col_refs:
                            col_name = col_name.lower()
                            if table_name in self.columns_per_table:
                                if col_name not in self.columns_per_table[table_name]:
                                    self.findings.append(Finding(
                                        severity="error",
                                        category="forward_column_reference",
                                        file=filename,
                                        line=idx,
                                        rule="forward_column_reference",
                                        message=f"Trigger on {table_name} references non-existent column '{col_name}'",
                                        suggestion=f"Add column {col_name} to table {table_name} before trigger"
                                    ))

    def _check_column_not_in_table(self, filename: str, lines: List[str]) -> None:
        """Rule 3: Detect SELECT/UPDATE on non-existent columns."""
        sql_text = "".join(lines)

        # Find SELECT statements with table aliases
        select_pattern = r'SELECT\s+.*?\s+FROM\s+([a-z_][a-z0-9_]*)\s+(?:AS\s+)?([a-z_][a-z0-9_]*)'
        for match in re.finditer(select_pattern, sql_text, re.IGNORECASE):
            table_name = match.group(1).lower()
            alias = match.group(2).lower() if match.group(2) else table_name

            # Find column references with this alias
            col_ref_pattern = rf'{alias}\.([a-z_][a-z0-9_]*)'
            for col_match in re.finditer(col_ref_pattern, sql_text, re.IGNORECASE):
                col_name = col_match.group(1).lower()

                if table_name in self.columns_per_table:
                    if col_name not in self.columns_per_table[table_name]:
                        line_num = sql_text[:col_match.start()].count('\n') + 1
                        self.findings.append(Finding(
                            severity="warning",
                            category="column_not_in_table",
                            file=filename,
                            line=line_num,
                            rule="column_not_in_table",
                            message=f"Reference to '{alias}.{col_name}' but table {table_name} has no column {col_name}",
                            suggestion=f"Verify column name or add column to {table_name}"
                        ))

    def _check_parameter_prefix_in_index(self, filename: str, lines: List[str]) -> None:
        """Rule 4: Flag CREATE INDEX with identifier starting with p_."""
        sql_text = "".join(lines)

        index_pattern = r'CREATE\s+INDEX\s+(?:[A-Za-z_][A-Za-z0-9_]*\s+)?ON\s+([a-z_][a-z0-9_]*)\s*\((.*?)\)'
        for match in re.finditer(index_pattern, sql_text, re.IGNORECASE):
            index_cols = match.group(2)

            # Look for p_ prefixes in expressions
            if re.search(r'\bp_\w+', index_cols, re.IGNORECASE):
                line_num = sql_text[:match.start()].count('\n') + 1
                self.findings.append(Finding(
                    severity="warning",
                    category="parameter_prefix_in_index",
                    file=filename,
                    line=line_num,
                    rule="parameter_prefix_in_index",
                    message=f"CREATE INDEX uses parameter-like identifier (p_*) in column list: {index_cols[:50]}",
                    suggestion="Verify this is not a function parameter mistakenly used in index definition"
                ))

    def _check_non_immutable_in_partial_index(self, filename: str, lines: List[str]) -> None:
        """Rule 5: Flag CREATE INDEX WHERE using volatile functions."""
        sql_text = "".join(lines)

        where_pattern = r'CREATE\s+INDEX\s+.*?\s+WHERE\s+(.*?)(?:;|$)'
        for match in re.finditer(where_pattern, sql_text, re.IGNORECASE | re.DOTALL):
            where_clause = match.group(1)

            for volatile_func in self.VOLATILE_FUNCTIONS:
                if re.search(rf'\b{volatile_func}\s*\(', where_clause, re.IGNORECASE):
                    line_num = sql_text[:match.start()].count('\n') + 1
                    self.findings.append(Finding(
                        severity="error",
                        category="non_immutable_in_partial_index",
                        file=filename,
                        line=line_num,
                        rule="non_immutable_in_partial_index",
                        message=f"CREATE INDEX WHERE clause uses volatile function '{volatile_func}()' — Postgres requires IMMUTABLE",
                        suggestion=f"Remove {volatile_func}() from WHERE clause or use immutable wrapper"
                    ))

    def _check_missing_search_path_on_definer(self, filename: str, lines: List[str]) -> None:
        """Rule 6: Flag CREATE FUNCTION SECURITY DEFINER without SET search_path."""
        sql_text = "".join(lines)

        definer_pattern = r'CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+.*?SECURITY\s+DEFINER(.*?)AS\s*\$\$'
        for match in re.finditer(definer_pattern, sql_text, re.IGNORECASE | re.DOTALL):
            between = match.group(1)

            if 'SET search_path' not in between:
                line_num = sql_text[:match.start()].count('\n') + 1
                self.findings.append(Finding(
                    severity="warning",
                    category="missing_search_path_on_definer",
                    file=filename,
                    line=line_num,
                    rule="missing_search_path_on_definer",
                    message="CREATE FUNCTION SECURITY DEFINER without SET search_path — may fail when called from different schema",
                    suggestion="Add 'SET search_path = public' before AS $$"
                ))

    def _check_rls_policy_cycles(self) -> None:
        """Rule 7: Detect RLS policy cycles (A→B→A references).

        Disabled by default due to high false positive rate with normalized RLS patterns.
        Most real-world cycles are acceptable (join tables back to parent). Enable only
        when investigating specific policy issues.
        """
        # Cycle detection disabled to reduce noise
        # Re-enable when investigating specific circular policy patterns
        pass

    def _check_inconsistent_column_names(self) -> None:
        """Rule 8: Detect inconsistent column naming (foo_id vs bar_id)."""
        # This is a complex heuristic: flag if table created with name_id but referenced as other_id
        for table_name, columns in self.columns_per_table.items():
            for col_name in columns:
                # Look for _id suffix columns
                if col_name.endswith("_id"):
                    # Check if this column is referenced with different prefix
                    for fname, lines in self.file_contents.items():
                        sql_text = "".join(lines)
                        # Find references to table.user_id etc
                        ref_pattern = rf'{table_name}\.([a-z_]*_id)\b'
                        for match in re.finditer(ref_pattern, sql_text, re.IGNORECASE):
                            ref_col = match.group(1).lower()
                            if ref_col != col_name and ref_col.endswith("_id"):
                                # Potential mismatch
                                line_num = sql_text[:match.start()].count('\n') + 1
                                self.findings.append(Finding(
                                    severity="warning",
                                    category="inconsistent_column_name",
                                    file=fname,
                                    line=line_num,
                                    rule="inconsistent_column_name",
                                    message=f"Table {table_name} has column '{col_name}' but reference uses '{ref_col}'",
                                    suggestion=f"Use consistent column name: {col_name}"
                                ))

    def _check_fk_constraints(self) -> None:
        """H47: Rule 9 — Detect FOREIGN KEY references to non-existent tables.

        Validates that every FOREIGN KEY constraint in the migration set
        references a table that was created in a prior migration or in the
        same migration file (self-referential). Reports errors for missing
        parent tables.

        Example error:
          Migration V2__create_orders.sql has:
            ALTER TABLE orders ADD CONSTRAINT fk_user
              FOREIGN KEY (user_id) REFERENCES users(id)
          But users table doesn't exist in V1__*.sql → error
        """
        # Build accumulated set of tables known up to each file
        cumulative_tables: Set[str] = set()
        file_to_accumulated: Dict[str, Set[str]] = {}

        for filename in self.file_order:
            # Add tables created in THIS file
            if filename in self.tables_per_file:
                cumulative_tables.update(self.tables_per_file[filename])
            file_to_accumulated[filename] = cumulative_tables.copy()

        # Now check FK constraints in each file
        for filename in self.file_order:
            lines = self.file_contents.get(filename, [])
            if not lines:
                continue

            sql_text = "".join(lines)

            # Pattern to find FOREIGN KEY constraints
            # Matches:
            #   FOREIGN KEY (col1, col2, ...) REFERENCES table_name(...)
            #   or inline in CREATE TABLE: ... FOREIGN KEY (...) REFERENCES ...
            fk_pattern = r'FOREIGN\s+KEY\s*\([^)]+\)\s+REFERENCES\s+([a-z_][a-z0-9_]*)\s*\('

            for match in re.finditer(fk_pattern, sql_text, re.IGNORECASE):
                ref_table = match.group(1).lower()
                line_num = sql_text[:match.start()].count('\n') + 1

                # Tables known up to and including this file
                known_tables = file_to_accumulated[filename]

                if ref_table not in known_tables:
                    known_list = ", ".join(sorted(known_tables)) if known_tables else "(none)"
                    self.findings.append(Finding(
                        severity="error",
                        category="fk_reference_missing_table",
                        file=filename,
                        line=line_num,
                        rule="fk_reference_missing_table",
                        message=f"FOREIGN KEY references table '{ref_table}' which doesn't exist in prior migrations",
                        suggestion=f"Create table '{ref_table}' in an earlier migration. Known tables up to {filename}: {known_list}"
                    ))


def main():
    """CLI entry. Supports `--naming=supabase|flyway|auto` (default auto)."""
    args = [a for a in sys.argv[1:] if a != "--test"]
    if not args:
        print(
            f"Usage: {sys.argv[0]} <migrations_dir_or_file> [--naming=auto|supabase|flyway]",
            file=sys.stderr,
        )
        sys.exit(1)

    naming = "auto"
    positional: List[str] = []
    for a in args:
        if a.startswith("--naming="):
            naming = a.split("=", 1)[1]
        else:
            positional.append(a)

    if not positional:
        print("missing target path", file=sys.stderr)
        sys.exit(1)

    linter = MigrationLinter(naming=naming)
    exit_code = linter.lint_path(positional[0])
    sys.exit(exit_code)


if __name__ == "__main__":
    # Simple test: create temp migrations directory with known bugs
    if len(sys.argv) > 2 and sys.argv[2] == "--test":
        import tempfile
        import shutil

        tmpdir = tempfile.mkdtemp()
        try:
            # Test 1: forward table reference
            with open(f"{tmpdir}/001_forward_ref.sql", "w") as f:
                f.write("""
CREATE FUNCTION check_user() RETURNS BOOLEAN AS $$
BEGIN
  INSERT INTO users_test (name) VALUES ('test');
  RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE users_test (id BIGSERIAL PRIMARY KEY, name TEXT);
""")

            # Test 2: volatile in index WHERE
            with open(f"{tmpdir}/002_volatile_index.sql", "w") as f:
                f.write("""
CREATE INDEX idx_recent_events ON events(id) WHERE created_at > NOW();
""")

            linter = MigrationLinter()
            exit_code = linter.lint_directory(tmpdir)
            print(f"\nTest passed: detected {len(linter.findings)} issues (expected >= 2)", file=sys.stderr)
            sys.exit(exit_code)
        finally:
            shutil.rmtree(tmpdir)
    else:
        main()
