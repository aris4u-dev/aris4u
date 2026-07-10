# Migration Linter (shipped in V16.3)

Detects SQL migration bugs before applying to production database.

## Quick Start

```bash
python3 migration_linter.py ~/projects/your-project/supabase/migrations/
```

Exit code: 0 if clean, 1 if errors detected.

## Rules

| Rule | Severity | Example | Fix |
|------|----------|---------|-----|
| **forward_table_reference** | ERROR | Function references table created later | Move CREATE TABLE before function |
| **forward_column_reference** | ERROR | Trigger uses column before ALTER ADD | Add column before trigger |
| **column_not_in_table** | WARNING | Query references nonexistent column | Fix column name or add column |
| **parameter_prefix_in_index** | WARNING | CREATE INDEX uses `p_` identifiers | Verify not function parameters |
| **non_immutable_in_partial_index** | ERROR | CREATE INDEX WHERE NOW() | Use immutable wrapper |
| **missing_search_path_on_definer** | WARNING | SECURITY DEFINER without search_path | Add SET search_path = public |
| **rls_policy_cycle** | - | DISABLED (too many false positives) | - |
| **inconsistent_column_name** | WARNING | Table has user_id, query uses account_id | Use consistent naming |

## Examples

### Forward Table Reference
```sql
-- WRONG: function uses rides table before it's created
CREATE FUNCTION check_ride() RETURNS BOOLEAN AS $$
BEGIN
  INSERT INTO rides (driver_id) VALUES (123);
  RETURN TRUE;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE rides (id BIGSERIAL, driver_id UUID);

-- CORRECT: create table first
CREATE TABLE rides (id BIGSERIAL, driver_id UUID);

CREATE FUNCTION check_ride() RETURNS BOOLEAN AS $$
BEGIN
  INSERT INTO rides (driver_id) VALUES (123);
  RETURN TRUE;
END;
$$ LANGUAGE plpgsql;
```

### Non-Immutable in Partial Index
```sql
-- WRONG: NOW() is volatile, Postgres rejects
CREATE INDEX idx_recent ON events(id) WHERE created_at > NOW();

-- CORRECT: use immutable comparison
CREATE INDEX idx_recent ON events(id) 
WHERE created_at > CURRENT_DATE;
```

### Missing search_path on DEFINER
```sql
-- WRONG: may fail when called from different schema
CREATE FUNCTION update_user(u_id UUID) RETURNS VOID AS $$
BEGIN
  UPDATE auth.users SET email = 'test@test.com' WHERE id = u_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- CORRECT: add search_path
CREATE FUNCTION update_user(u_id UUID) RETURNS VOID AS $$
BEGIN
  UPDATE auth.users SET email = 'test@test.com' WHERE id = u_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;
```

## Output Format

JSONL (JSON Lines) format, one finding per line:

```json
{
  "severity": "error",
  "category": "non_immutable_in_partial_index",
  "file": "032_rate_limiting.sql",
  "line": 63,
  "rule": "non_immutable_in_partial_index",
  "message": "CREATE INDEX WHERE uses NOW() — Postgres requires IMMUTABLE",
  "suggestion": "Use immutable wrapper function or different index strategy"
}
```

## Integration

Use in CI/CD before migrations apply:

```bash
# Exit 0 if clean, 1 if errors
python3 migration_linter.py ./migrations/ || exit 1
```

Parse output in scripts:

```bash
python3 migration_linter.py ./migrations/ | \
  jq -r 'select(.severity == "error") | .message'
```

## Known Limitations

- Rule 7 (RLS policy cycles) disabled due to false positives
- Column detection uses simplified patterns (not full SQL parser)
- Forward references only checked within same file
- Does not validate trigger syntax or function bodies

## Dataset

Tested against a Supabase project with 39 migration files (4207 lines):
- Detected 3 errors, 49 warnings
- 32 column_not_in_table (mostly from aliases)
- 9 missing_search_path_on_definer (rule 6)
- 8 inconsistent_column_name (rule 8)
- 2 forward_table_reference (rule 1)
- 1 non_immutable_in_partial_index (rule 5)

All findings are real issues or false positives worth reviewing.
