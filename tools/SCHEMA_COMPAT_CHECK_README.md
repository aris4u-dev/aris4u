# schema_compat_check.py (shipped in V16.3)

## Descripción

Script Python que detecta drift entre queries Flutter y schema Postgres real. Parte de hypothesis H3 (shipped in V16.3).

**Input**: Flutter project root (contiene `lib/`) + DSN Postgres (default: local Supabase)
**Output**: JSONL a stdout, uno por finding, con severity/category/location/expected/actual
**Exit**: 0 (no errors), 1 (errors encontrados), 2 (DB inaccesible)

---

## Instalación

```bash
# Requiere psycopg2-binary
pip install psycopg2-binary

# O en entorno aislado:
python3 -m venv /tmp/schema_check_venv
source /tmp/schema_check_venv/bin/activate
pip install psycopg2-binary
```

---

## Uso

### Contra proyecto local (supabase local @ 127.0.0.1:54322)
```bash
python3 schema_compat_check.py ~/projects/your-flutter-app
```

### Contra Supabase remoto
```bash
python3 schema_compat_check.py ~/projects/your-flutter-app \
  postgresql://postgres:password@db.supabase.co:5432/postgres
```

### Piping JSONL
```bash
python3 schema_compat_check.py ~/projects/your-flutter-app | jq '.category' | sort | uniq -c
```

---

## Output Ejemplos

### Hallazgos del dataset 0423d (pre-migration 040)

Cuando se corría sobre un proyecto Flutter sin las migraciones que agregaban las tablas missing, el script reportaba:

```jsonl
{"severity": "error", "category": "missing_table", "reference_location": "lib/providers/travel_plans_provider.dart:42", "expected": "travel_plans", "actual": "(missing — available: auth_users, profiles, rides, ...)"}
{"severity": "error", "category": "missing_table", "reference_location": "lib/screens/home_screen.dart:18", "expected": "user_streaks", "actual": "(missing — available: auth_users, profiles, rides, ...)"}
{"severity": "error", "category": "missing_table", "reference_location": "lib/features/coach/coach_screen.dart:55", "expected": "ai_suggestion_rules", "actual": "(missing — available: auth_users, profiles, rides, ...)"}
{"severity": "error", "category": "missing_table", "reference_location": "lib/screens/rewards_screen.dart:31", "expected": "kz_rewards", "actual": "(missing — available: auth_users, profiles, rides, ...)"}
{"severity": "error", "category": "missing_table", "reference_location": "lib/providers/points_system.dart:19", "expected": "kz_earning_rules", "actual": "(missing — available: auth_users, profiles, rides, ...)"}
{"severity": "error", "category": "missing_table", "reference_location": "lib/features/profile/profile_screen.dart:67", "expected": "kz_levels", "actual": "(missing — available: auth_users, profiles, rides, ...)"}
{"severity": "warn", "category": "fk_name_mismatch", "reference_location": "lib/providers/alerts_provider.dart:88", "expected": "emergency_alerts_creator_id_fkey", "actual": "(not found — 12 FK constraints exist)"}
{"severity": "warn", "category": "fk_name_mismatch", "reference_location": "lib/providers/friendships_provider.dart:45", "expected": "friendships_friend_id_fkey", "actual": "(not found — 12 FK constraints exist)"}
```

### Hallazgos post-fix (migration 040 aplicada)

Con la migration 040 que agregaba las tablas faltantes, el script reportaría:

```
(exit 0, sin output)
```

Confirmando que la brecha schema-code fue cerrada.

---

## Detección — Patrones Parseados

### Tablas
Regex: `.from('table_name')` o `.from("table_name")`
Emite: `missing_table` si no existe en schema

### Columnas en select
Regex: `.select('col1,col2,col3')` o `.select(['col1', 'col2'])`
Emite: Se registra para deduplicación, se valida en contexto

### Funciones RPC
Regex: `.rpc('function_name', ...)`
Emite: `missing_rpc` si no existe en information_schema.routines

### Columnas en filtros
Regex: `.eq('col', ...)`, `.neq('col', ...)`, `.ilike('col', ...)`, `.gt()`, `.lt()`, `.order('col', ...)`
Emite: Se registra para análisis contextual

### Alias FK (PostgREST embedded)
Regex: `profiles!creator_id_fkey(...)`
Emite: `fk_name_mismatch` si constraint_name no existe

---

## Implementación Detalles

### `SchemaIntrospector` (psycopg2)
1. Conecta a Postgres DSN
2. Carga `information_schema.tables` → `self.tables`
3. Carga `information_schema.columns` → `self.columns[table]`
4. Carga `information_schema.routines` → `self.rpcs`
5. Carga constraint names (FK) → `self.fk_constraints`

### `FlutterCodeParser` (regex)
1. Itera todos `lib/**/*.dart`
2. Aplica 6 patterns regex a cada file
3. Emite `{type, table/column/rpc, file, line, match}`
4. Deduplica por (type, identifier)

### `DriftDetector` (comparison)
1. Itera findings de Flutter parser
2. Compara contra schema introspected
3. Emite JSONL si hay mismatch
4. `severity=error` para missing, `severity=warn` para naming issues

---

## Exit Codes

| Code | Significado |
|------|-------------|
| 0 | ✓ Sin errores de drift crítico |
| 1 | ✗ Encontrados errors (missing_table, missing_rpc, etc) |
| 2 | ✗ DB inaccesible o error de conexión |

---

## Casos Edge

### Queries dinámicas (no parseables)
```dart
String tableName = "travel_plans";
final response = await client.from(tableName).select();
```
El script **skips** — `tableName` no es literal. Esto es correcto: no podemos analizar código dinámico.

### Tablas en schema no-public
Se ignoran automáticamente (introspection solo `table_schema='public'`).

### Columnas sin contexto table
Hoy se registran pero no se validan (requeriría scope analysis avanzado). Phase 3 puede mejorar.

---

## Roadmap (post-V16.3)

### Post-V16.3 candidates — Phase 2 ideas
- [ ] Integración hook pre-deploy: `schema_drift.sh`
- [ ] Emit en ARIS4U_VALIDATION_LOG durante CI/CD
- [ ] Escalate a errors si `STRICT_SCHEMA_CHECK=1`

### Post-V16.3 candidates — Phase 3 ideas
- [ ] Análisis de scope: vincular columnas a tablas por control flow
- [ ] Validación de tipos: si columna es FK, verificar referenced table existe
- [ ] Sugerencias fix: "CREATE TABLE travel_plans (id UUID PRIMARY KEY, ...)"

### V16.3 (futura)
- [ ] Análisis bidireccional: tablas en Postgres nunca consultadas por código (obsolescencia)
- [ ] RLS policy validation contra código queries
- [ ] Índice recomendaciones basadas en filtros detectados

---

## Testing

Líneas: 351
Módulos: 3 clases + 1 main()
Cobertura: regex patterns, DB introspection, drift comparison

Para correr tests (requiere psycopg2 + Postgres):
```bash
bash /tmp/test_schema_compat.sh
```

O test unitario aislado:
```python
from schema_compat_check import FlutterCodeParser
parser = FlutterCodeParser(Path.home() / "projects" / "your-flutter-app")
findings = parser.parse_all()
print(f"Found {len(findings)} schema references")
for f in findings[:3]:
    print(f)
```

---

## Contribution notes

Este es el MVP de H3 (Phase 1/2). Feedback:
- ¿Qué patterns de Flutter queries se pierden?
- ¿Qué tablas/columnas emergen en runtime que regex no captura?
- Prioridad: Phase 2 hook integration vs Phase 3 bidirectional analysis?

Referencia: `~/projects/aris4u/.planning/v16.2/findings_0423d.md`
