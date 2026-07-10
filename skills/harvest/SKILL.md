---
name: harvest
description: >
  Cierre de bloque de trabajo: cosecha journals de workflow terminados, detecta y
  (con confirmación) mata procesos huérfanos —pytest, ollama runners colgados, npm,
  builds zombis— y lista sesiones Claude posiblemente colgadas por etime alto, SIN
  matarlas a ciegas. Deber #3 del modo orquestador ("no dejar NADA abandonado").
  Use when: (1) al terminar un workflow o un bloque pesado, (2) "limpia / cosecha /
  hay huérfanos", (3) el M5 se siente cargado tras varios fan-outs, (4) /preflight
  dio NO-GO. NUNCA mata sin confirmar (una sesión puede ser la viva).
---

# /harvest — Cierre de bloque: cosechar y limpiar

Recupera lo dejado en vuelo y limpia huérfanos con disciplina. **Nunca mata a
ciegas** — primero reporta, distingue por PPID/uptime, y solo mata lo confirmado.

## Cómo ejecutar

Fase 1 — diagnóstico (read-only, siempre primero):
```bash
bash ~/.claude/bin/harvest.sh
```

Reporta:
1. **Journals de workflow** terminados sin cosechar (para leer sus resultados).
2. **Procesos huérfanos candidatos** — pytest / `ollama runner` / vite / esbuild /
   node de builds, con PID, PPID, RSS y etime. Marca PPID=1 (reparentados = huérfanos
   probables) vs PPID vivo.
3. **Sesiones Claude** por etime — distingue la de ESTA sesión (no tocar) de las de
   etime alto sin actividad.

## Matar huérfanos (solo tras revisar la fase 1)

El script NO mata por sí solo. Tras leer el reporte, mata explícito lo confirmado:
```bash
kill <PID>          # un huérfano concreto verificado
```
Regla dura: **jamás** `pkill -f claude` ni matar por nombre en bloque — una de esas
sesiones puede ser la activa. Confirmar PID por PID.

## Por qué

Regla de oro #1 (parallel-dispatch §MODO POR DEFECTO, deber #3): un workflow a la vez,
cosechar journals y matar huérfanos al cerrar cada bloque. El incidente 79-92 agentes /
104 workflows (2026-06-16) nació de acumular sin cosechar.
