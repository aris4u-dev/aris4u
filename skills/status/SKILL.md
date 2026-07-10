---
name: status
description: >
  Estado vivo y verificado de cualquier proyecto registrado. Regenera desde la verdad
  viva (git, versiones, métricas, tests + pruebas ejecutables por componente) y reporta
  el STATUS.md. NUNCA confíes en docs/HTML/MEMORY.md sueltos: corre esto. Use when:
  (1) "¿está listo X?" / "¿en qué va X?", (2) "estado/status de X", (3) al EMPEZAR
  a trabajar en un proyecto para aterrizar en la verdad actual, (4) tras cerrar un
  hito para refrescar el status.
---

# /status — Estado vivo y auto-verificado por proyecto

## Qué es

Mata el problema de "siento que empezamos de 0". La verdad del status se **genera**
desde la realidad viva (no se escribe a mano → no driftea). Una sola fuente por
proyecto: `STATUS/manifest.yaml`. Todo lo demás se deriva y se sella con timestamp.

- `<repo>/STATUS.md` — documento único con el 100% de la verdad (léelo PRIMERO).
- `<repo>/STATUS/status.json` — cada cosa por separado (máquina).
- `<repo>/STATUS/index.html` — dashboard visual fresco.
- `~/Claude/docs/STATUS.md` — índice portfolio de todos los proyectos.

Cada componente/workstream lleva una `proof` (comando shell). El generador la corre:
- 🟢 **verified** — la declaración coincide con la evidencia viva HOY.
- 🔴 **misaligned** — el claim NO coincide con la realidad. **Bandera roja: investiga.**
- ⚪ **unproven** — declarado sin prueba ejecutable.

## Cómo usar

1. **Regenera** (rápido, sin correr la suite de tests):
   ```bash
   python3 ~/.claude/bin/status-gen.py <proyecto>
   ```
   Con la suite de tests (más lento, ~minutos según el proyecto):
   ```bash
   python3 ~/.claude/bin/status-gen.py <proyecto> --tests
   ```
   Todos los registrados de una: `python3 ~/.claude/bin/status-gen.py --all`

2. **Lee** `<repo>/STATUS.md` y reporta al usuario:
   - Resumen (componentes/WS hechos, tests).
   - **Cualquier 🔴 desalineado va PRIMERO** — es el claim que ya no es cierto.
   - Git: rama, HEAD, dirty, último commit.

3. Si el usuario quiere el visual, abre/comparte `<repo>/STATUS/index.html`.

`<proyecto>` puede ser un id registrado (ej. `aris4u`) o la ruta del repo. La
primera vez para un proyecto nuevo, pasa la **ruta del repo** (se auto-registra).

## Añadir un proyecto nuevo

Crea `<repo>/STATUS/manifest.yaml` (copia el de ARIS4U como plantilla:
`~/projects/aris4u/STATUS/manifest.yaml`). Declara:
- `project`, `name`, `tagline`, `repo`, `identity`.
- `auto:` → `git: true`, `versions:` (file + json_key|regex), `metrics:` (label+cmd),
  `tests:` (cmd + timeout).
- `components:` y `workstreams:` → cada uno con `status` (done|in_progress|blocked|
  pending) y una `proof` ejecutable (o `null` si honestamente no hay prueba).
- Opcional `mirror_html:` para volcar el dashboard a una ruta extra (ej. Desktop).

Luego: `python3 ~/.claude/bin/status-gen.py <repo>` para registrarlo y generar.

## Regla de oro

El status SOLO es cierto si se regeneró hoy. Si el `generated_at` del STATUS.md es
viejo, **regenera antes de afirmar nada** (alineado con CLAIM-VERIFY / DOCS-FRESH).
