# ARIS4U Hook Dispatcher

**Un solo entrypoint por evento de Claude Code.** En lugar de N scripts `.sh` por evento,
`~/.claude/settings.json` y `hooks/hooks.json` cablean **7 eventos** a un único proceso Python:

```
.venv312/bin/python3 hooks/dispatch.py <Event>     # stdin = payload JSON del evento
```

`hooks/dispatch.py` lee el payload, resuelve el handler en `HANDLERS`
(`hooks/dispatch/events/__init__.py`) y aplica el contrato (advisory / bloqueo / deny).
**Fail-open:** cualquier error de infra o de un handler → `passthrough()` (exit 0). Un evento
sin handler cae a no-op, así la migración es incremental.

> Los `.sh` originales fueron **portados a Python** y están **MUERTOS** (no cableados a nada).
> Editar el `.py`, nunca el `.sh`.

---

## Mapa evento → módulo handler

`hooks/dispatch/events/__init__.py` registra los 7 eventos. Cada evento es un módulo en
`hooks/dispatch/events/` con una función `handle(event_name, payload)`:

| Evento Claude Code | Módulo (`events/`) | Qué hace |
|---|---|---|
| `SubagentStart` | `subagent_start.py` | Inyecta decisiones lockeadas + guards críticos al sub-agente |
| `SessionStart` | `session_start.py` | Self-briefing + recall lab + puente de cliente MCP + reset de presupuesto |
| `SessionEnd` | `session_end.py` | Captura digest de sesión + commits |
| `Stop` | `stop.py` | Cierre de turno |
| `UserPromptSubmit` | `user_prompt_submit.py` | auto_recall + model_hint por prompt |
| `PostToolUse` | `post_tool_use.py` | Combina 5 sub-handlers (ver abajo) |
| `PreToolUse` | `pre_tool_use.py` | Encadena 11 sub-handlers en orden (ver abajo) |

Los **sub-handlers** (las piezas puras que orquestan PreToolUse/PostToolUse) viven en
`hooks/dispatch/handlers/`. Helpers de soporte (no event-handlers): `verdict.py` (el tipo
`Verdict`: PASS / ADVISE / BLOCK / DENY), `pre_common.py`, `pre_guards.py` (7 guards de
estándar en un módulo). El contrato de salida está en `hooks/dispatch/contract.py`
(`read_event`, `passthrough`, `advise`, `block`, `ARIS4U_ROOT`).

### PreToolUse — cadena de 11 sub-handlers (orden = settings.json)

`pre_tool_use.py` los corre **en orden**. El **primer** veredicto de bloqueo corta la cadena;
los advisory se acumulan y se emiten juntos al final (un solo `additionalContext`, exit 0).

| # | Sub-handler | Matcher | Veredicto |
|---|---|---|---|
| 1 | `f5_prevalidation` | Write\|Edit | advisory (shadow) |
| 2 | `migration_linter` | Bash | 🔴 **BLOQUEANTE** (BLOCK, exit 2) |
| 3 | `phi_guard` | Bash\|WebFetch\|WebSearch | 🔴 **BLOQUEANTE** (BLOCK, exit 2 — solo healthcare) |
| 4 | `phi_sanitizer` | Bash\|Write\|Edit\|Read | advisory (healthcare) |
| 5 | `type_hints` | Write\|Edit (`*.py`) | advisory |
| 6 | `docker_latest` | Write\|Edit (Dockerfile/compose) | advisory |
| 7 | `supabase_rls` | Write\|Edit (`*.sql`) | advisory |
| 8 | `spring_boot` | Write\|Edit (`*.java`) | advisory |
| 9 | `screenshot_loop` | Bash | advisory |
| 10 | `kb_docs` | Write\|Edit (`Claude/docs/*.md`) | advisory |
| 11 | `gpu_crash` | Bash / playwright navigate | 🔴 **DENY** (`permissionDecision:"deny"`, exit 0) |

Sub-handlers 5–11 viven en `handlers/pre_guards.py`; 1–4 en sus propios módulos
(`f5_prevalidation.py`, `migration_linter.py`, `phi_guard.py`, `phi_sanitizer.py`).

### PostToolUse — combina 5 sub-handlers

`post_tool_use.py` corre todos respetando su matcher y emite **un** JSON combinado:

| Sub-handler | Matcher | Efecto |
|---|---|---|
| `redact` (redact_secrets) | Bash | **muta** output (`updatedToolOutput`) |
| `parallel_dispatch_guard` | Write/Edit a `*.sh`/`*.bash` | advisory `additionalContext` |
| `capture_commit` | Bash + `git commit` | side-effect (decisión en `sessions.db`) |
| `agent_dispatched` | Agent/Task | side-effect (snapshot JSONL) |
| `schema_drift` | Write/Edit/MultiEdit | telemetría + warn a stderr |

Ninguno bloquea (advisory total, exit 0).

---

## Bloqueantes reales

- **`migration_linter`** — BLOCK (exit 2) si una migración tiene errores.
- **`phi_guard`** — BLOCK (exit 2) si hay PHI → destino externo, **solo en healthcare**.
- **`gpu_crash`** — DENY (`permissionDecision:"deny"`, exit 0) sobre el viewer splat WebGL.

Todo lo demás es **advisory** (acumula `additionalContext`, nunca corta el tool).

---

## `.sh` vivos vs muertos

**Vivos** (siguen siendo `.sh`, invocados desde Python o cron — NO portados):

| `.sh` vivo | Quién lo llama |
|---|---|
| `hooks/write_client_bridge.sh` | `events/session_start.py` (puente de cliente para el demonio MCP) |
| `hooks/async_vacuum.sh` | mantenimiento de DB (async) |
| `hooks/nightly_vacuum.sh` | cron nocturno de vacuum |

**Muertos** (portados a `hooks/dispatch/` — el `.sh` ya **no está cableado a nada**; editar el `.py`):

```
agent_dispatched.sh        capture_commit.sh        depth_inject.sh
f5_prevalidation.sh        lab_session_init.sh      migration_linter.sh
phi_guard.sh               phi_sanitizer.sh         post_agent_verify.sh
redact_secrets.sh          schema_drift.sh          session_end.sh
subagent_depth.sh          guards/gpu-crash-guard.sh
guards/parallel-dispatch-guard.sh
```

**INERTE** — `hooks/orchestrator_enforcer.sh`: **nunca portado ni cableado**. No tiene caller
en ningún `.py`, en `settings.json` ni en `hooks.json` (solo aparece en auditorías históricas
bajo `architecture/audits/`). No existe handler equivalente. Tratar como muerto.

---

## Cómo añadir un hook

1. Escribe un **handler puro** en `hooks/dispatch/handlers/<nombre>.py`:
   una función `(tool_name, tool_input[, cwd]) -> Verdict` (PreToolUse) o con side-effect
   (PostToolUse). No hace `sys.exit`; devuelve un `Verdict` (ver `handlers/verdict.py`):
   `V.ok()` / `V.advise(msg)` / `V.block(msg)` / `V.deny(reason)`.
2. **Regístralo** en el módulo de evento (`events/pre_tool_use.py` lo añade a la `chain`;
   `events/post_tool_use.py` lo invoca en `handle`). Para un evento nuevo: crea
   `events/<evento>.py` con `handle(...)` y añádelo a `HANDLERS` en `events/__init__.py`.
3. Si el evento aún no está cableado, añádelo a `hooks/hooks.json` (y al `settings.json`
   global) apuntando a `dispatch.py <Event>`.

## Cómo depurar un hook

Pasa un payload JSON por stdin al dispatcher con el nombre del evento:

```bash
echo '{}' | .venv312/bin/python3 hooks/dispatch.py PreToolUse
echo '{"tool_name":"Write","tool_input":{"file_path":"x.py","content":"def f(x):\n    return x"}}' \
  | .venv312/bin/python3 hooks/dispatch.py PreToolUse
```

La salida es el JSON del contrato (advisory `additionalContext`, `permissionDecision`, o nada).
Un BLOCK sale con exit 2 y mensaje a stderr; un DENY sale con exit 0 y `permissionDecision:"deny"`.
Como todo es fail-open, un handler que crashea no rompe el tool — la cadena continúa.

---

## ⚠️ Hazard de nombres: `dispatch.py` vs `dispatch/`

En `hooks/` conviven **dos entidades con el mismo nombre raíz**:

| Entidad | Ruta | Rol |
|---------|------|-----|
| `hooks/dispatch.py` | archivo Python | **Entrypoint CLI** — Claude Code lo invoca vía `python hooks/dispatch.py <Event>` |
| `hooks/dispatch/` | paquete Python | **Paquete interno** con eventos, handlers, contratos y contrato de salida |

### Por qué es frágil

Python resuelve `import dispatch` buscando primero **paquetes** (`hooks/dispatch/__init__.py`)
antes que módulos sueltos (`hooks/dispatch.py`) cuando el directorio de trabajo o `sys.path`
incluye `hooks/`. Si cualquier código dentro de `hooks/` ejecuta `import dispatch`, importará
el **paquete** `hooks/dispatch/`, no el entrypoint CLI — silenciosamente y sin error en el
momento de importación (solo falla si accede a atributos del CLI que no existen en el paquete).

### Regla permanente

**NUNCA hacer `import dispatch` desde ningún módulo dentro de `hooks/`.**

- Los event handlers (`hooks/dispatch/events/*.py`) y los sub-handlers
  (`hooks/dispatch/handlers/*.py`) no necesitan importar el entrypoint — usan importaciones
  relativas dentro del paquete (`.contract`, `.verdict`, etc.).
- El entrypoint `dispatch.py` se invoca como script (`python hooks/dispatch.py <Event>`),
  nunca como módulo importado.
- Si en el futuro se refactoriza el entrypoint, renombrarlo (p. ej. `dispatch_cli.py`) eliminaría
  la ambigüedad — pero esa operación requiere actualizar `hooks/hooks.json`, `~/.claude/settings.json`
  y el CLAUDE.md del proyecto. No renombrar sin coordinar esos tres archivos.

### Cómo detectar violaciones

```bash
# Buscar cualquier `import dispatch` (sin punto) dentro de hooks/
grep -r "^import dispatch\b" ${ARIS4U_ROOT}/hooks/
# Resultado esperado: vacío (ninguna violación)
```
