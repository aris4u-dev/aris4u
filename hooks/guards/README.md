# ARIS4U Convention Guards

**Actualizado:** 2026-06-16 — reescrito para el **dispatcher único**.
**Referencia:** `architecture/ARIS4U_MASTER.md` §1 + `hooks/dispatch/README.md` (mapa completo evento→handler).

---

## ⚠️ Cambio de arquitectura: los guards ya NO son `.sh` con activación manual

Versiones anteriores de este README describían ~8 scripts `.sh` en `hooks/guards/` que
había que cablear a mano en `~/.claude/settings.json`. **Eso ya no aplica.**

Hoy los guards viven como **sub-handlers Python** dentro del dispatcher único. El cableado
es automático: `settings.json` / `hooks/hooks.json` envían cada evento a
`.venv312/bin/python3 hooks/dispatch.py <Event>`, y el orquestador de PreToolUse
(`hooks/dispatch/events/pre_tool_use.py`) corre los guards en orden. No hay activación
manual; están vivos en la cadena.

En este directorio (`hooks/guards/`) **solo quedan vivos** dos `.sh`, y NO se cablean a mano:

- `gpu-crash-guard.sh` — **portado y MUERTO**; su lógica vive en `pre_guards.gpu_crash`.
- `parallel-dispatch-guard.sh` — **portado y MUERTO**; su lógica vive en
  `handlers/parallel_dispatch_guard.py` (PostToolUse).

Los otros 6 `.sh` que el README viejo listaba (`type-hints-guard.sh`,
`docker-latest-guard.sh`, `supabase-rls-guard.sh`, `spring-boot-pattern-guard.sh`,
`screenshot-loop-guard.sh`, `kb-docs-validator-guard.sh`, `astro-w2-only-guard.sh`) **ya no
existen como archivos sueltos aquí** — fueron absorbidos en
`hooks/dispatch/handlers/pre_guards.py`.

---

## Guards reales (sub-handlers vivos)

Los guards de estándar puros están en **`hooks/dispatch/handlers/pre_guards.py`**, cada uno
una función pura `(tool_name, tool_input) -> Verdict`. Se ejecutan vía la cadena de
PreToolUse (orden = `settings.json`). Todos son **advisory** salvo `gpu_crash`.

| Guard (función en `pre_guards.py`) | Aplica a | Detecta | Bloqueante |
|---|---|---|---|
| `type_hints` | Write/Edit `*.py` | funciones sin `-> ReturnType` | NO (advisory) |
| `docker_latest` | Write/Edit Dockerfile/compose | imágenes sin pin / `:latest` | NO (advisory) |
| `supabase_rls` | Write/Edit `*.sql` | `CREATE TABLE` sin `ENABLE ROW LEVEL SECURITY` | NO (advisory) |
| `spring_boot` | Write/Edit `*.java` | `@Autowired` (usar constructor injection) | NO (advisory) |
| `screenshot_loop` | Bash | ≥2 `screenshot` en un comando | NO (advisory) |
| `kb_docs` | Write/Edit `Claude/docs/*.md` | TODO/FIXME / falta header `Actualizado: YYYY-MM-DD` | NO (advisory) |
| `gpu_crash` | Bash / playwright navigate | abrir viewer splat WebGL (`.ply`/`.splat`/`:8901`) | 🔴 **SÍ — DENY** |

`gpu_crash` es el **único bloqueante** de este grupo: emite
`permissionDecision:"deny"` (exit 0) y respeta un override en
`~/.aris4u/gpu-crash-override` (degrada a advisory si existe).

Sub-handlers de seguridad relacionados, en sus propios módulos (no en `pre_guards.py`):

- `handlers/migration_linter.py` — 🔴 **BLOQUEANTE** (BLOCK, exit 2) en migraciones con errores.
- `handlers/phi_guard.py` — 🔴 **BLOQUEANTE** (BLOCK, exit 2) PHI→externo, **solo healthcare**.
- `handlers/phi_sanitizer.py`, `handlers/f5_prevalidation.py` — advisory.
- `handlers/parallel_dispatch_guard.py` — advisory en PostToolUse (Write/Edit a `*.sh`/`*.bash`).

**Bloqueantes reales en todo el dispatcher: `migration_linter` y `phi_guard` (BLOCK);
`gpu_crash` (DENY). Todo lo demás es advisory.**

---

## Principios de diseño

1. **Fail-open:** un guard que crashea NO bloquea ni corta la cadena (se ignora).
2. **Advisory por defecto:** acumulan `additionalContext` (exit 0). Solo 3 cortan el tool.
3. **Orden importa:** la cadena corre en el orden de `settings.json`; el primer BLOCK/DENY corta.
4. **Rápido:** regex/grep, sin subprocesos pesados.

## Cómo tocar un guard

Edita la función correspondiente en `hooks/dispatch/handlers/pre_guards.py` (o el módulo de
seguridad dedicado). **No edites los `.sh` muertos.** Para añadir uno nuevo, regístralo en la
`chain` de `hooks/dispatch/events/pre_tool_use.py`. Detalle completo: `hooks/dispatch/README.md`.

## Cómo depurar

```bash
echo '{"tool_name":"Write","tool_input":{"file_path":"x.py","content":"def f(x):\n    return x"}}' \
  | .venv312/bin/python3 hooks/dispatch.py PreToolUse
```

La salida esperada es un `additionalContext` con el aviso de type-hints (advisory, exit 0).
