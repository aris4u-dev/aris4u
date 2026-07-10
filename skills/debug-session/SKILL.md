---
name: debug-session
description: >
  Step-debug PROGRAMÁTICO y headless de un bug de RUNTIME en Python — el 4º peldaño de
  escalada, cuando un fallo resiste análisis estático + tests + aris_dialectic + logs.
  Usa `tools/debug_capture.py` (bdb stdlib, CERO deps, PHI-safe, corre local in-process)
  para poner breakpoints (por línea o condicionales) o capturar el estado en el punto de
  una EXCEPCIÓN (postmortem: locals + stack en el momento del throw), y analiza ese estado
  para aislar la causa. NO es un IDE interactivo ni VS Code: captura estado automática para
  que el LLM razone sobre él. Es el "debugger de VS Code sin VS Code" — el mismo valor
  (inspección de estado en runtime) sin la interacción humana ni el editor.
  Use when: (1) un bug de runtime que NO se ve desde el código estático ni desde los tests
  ni desde los logs — necesitas el valor de las variables en un punto concreto de ejecución;
  (2) una excepción cuya causa no es obvia y quieres el estado exacto en el punto del fallo;
  (3) "por qué esta función devuelve X", "captura el estado en la línea N", "qué valen las
  variables cuando falla". NO usar para: bugs que un test o un print resuelven (más barato
  primero), código NO-Python (Node/Dart diferido — ver Frontera), ni datos PHI reales.
---

# /debug-session — step-debug programático (4º peldaño de escalada)

Materializa la capacidad de inspección de estado en runtime SIN VS Code. Motor:
`~/projects/aris4u/tools/debug_capture.py` (bdb stdlib, cero dependencias, in-process,
PHI-safe). Corre desde el venv del proyecto (para ARIS4U: `.venv312`).

## Cuándo alcanzar esta skill (disciplina de escalada)

Es el **4º peldaño**, no el primero. Antes de invocarla, confirma que fallaron los baratos:

1. Análisis estático (ruff/pyright/grep) + leer el código.
2. Tests + `aris_dialectic` (review multi-rol).
3. Logs / `print` / `second-auditor`.
4. **→ Aquí:** el estado de runtime en un punto concreto es lo único que falta.

Debuggear es caro e intrusivo → **on-demand, NUNCA hook always-on**.

## Workflow

1. **Aterriza el bug**: identifica el archivo y la función objetivo, y formula una HIPÓTESIS
   de qué variable/estado está mal y DÓNDE (línea, o "en el punto de la excepción").

2. **Elige el modo de captura**:
   - **Excepción (postmortem)** — el caso más común: tienes una función/test que LANZA.
     Corre el target bajo `debug_capture` sin breakpoints; el hook de excepción captura
     locals + stack en el punto exacto del throw.
   - **Breakpoint por línea / condicional** — quieres el estado en una línea concreta,
     opcionalmente solo cuando se cumple una condición (`i == 3`, `total > 100`).

3. **Ejecuta** (desde el venv correcto, rutas ABSOLUTAS):
   ```bash
   cd <proyecto>
   .venv312/bin/python -m tools.debug_capture \
     --target 'modulo.submodulo:funcion' \
     --break '/abs/path/archivo.py:47' \
     --break '/abs/path/archivo.py:82:total>100' \
     --max-hits 5 --timeout 30
   ```
   O importable: `from tools.debug_capture import debug_capture` → `debug_capture(target, breakpoints=[(file,line,cond)], inputs=..., max_hits=..., timeout_s=...)` → dict JSON.
   Para un script (no función importable), el módulo lo envuelve con `runpy`.

4. **Analiza el JSON capturado**: compara el estado real (`locals`, `stack`, `exception`)
   contra tu hipótesis. El valor incorrecto en el punto de captura ES la pista de causa-raíz.
   Si no confirma la hipótesis, ajusta el breakpoint (más arriba en el stack) y repite.

5. **Cierra**: enuncia la causa-raíz con la evidencia del estado capturado + el fix mínimo.
   Si el hallazgo es reutilizable, persístelo con `aris_ingest` (decisión/guard por proyecto).
   In a LAB project (Lab-Project-1/Lab-Project-2), also: entry in the validation log (Law #0).

## Límites honestos (del motor)

- **`async def` no soportado** (bdb es `sys.settrace` síncrono). Para coroutines: envolver con
  `asyncio.run(...)` y poner el breakpoint en líneas síncronas dentro del coroutine.
- **Conflicto con `pytest-cov`** (ambos instrumentan `sys.settrace`): correr el target con
  `debug_capture` directamente sobre la función, sin `--cov` simultáneo.
- **Solo Python.** Node/Dart están DIFERIDOS (YAGNI). Si algún día hacen falta: NO cablear el
  `mcp-debugger` externo persistente — el scan lo marcó RCE + lectura-de-archivos arbitraria
  por diseño (peligroso en máquina con PHI/credenciales). Ruta futura: in-house CDP/DAP, o el
  MCP externo EFÍMERO (pineado `@0.22.0`, en sesión sin contexto PHI). Ver capability-map §B.

## Frontera
- Estado de runtime Python que no se ve estático → **/debug-session** (esta skill).
- Review de código → `aris_dialectic`. Gate de cierre → `/second-auditor`. Verificar claims → `/verify-claims`.
- Node/Dart runtime → DIFERIDO.
