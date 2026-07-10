---
name: aris-status
description: >
  Panel de status de capacidades de ARIS4U: hooks cableados (por evento), MCP tools,
  guards activos, conteos de memoria (decisions/guards/digests por cliente), modelo por
  defecto y últimos eventos de telemetría. Lee TODO en read-only (URI mode=ro, nunca
  escribe). Use when: (1) "¿está activo ARIS4U?" / "qué capacidades tengo", (2) "status
  de ARIS4U / del plugin / de los hooks", (3) verificar que hooks+MCP+guards están vivos,
  (4) ver la memoria por cliente. Es el motor de la Capa 0 del wrapper ARIS4U Desktop.
---

# /aris-status — Status de capacidades de ARIS4U

Muestra el estado vivo de la capa ARIS4U (hooks, MCP, guards, memoria) leyendo las
fuentes reales sin escribir en nada de producción.

## Cómo ejecutar

Corre el motor de status (stdlib, cualquier python3):

```bash
python3 ~/projects/aris4u/tools/aris_status.py
```

- `--no-color` para salida plana.
- `--json` para estructura consumible (la usan las Capas 1/2 del wrapper).

Imprime la salida tal cual al usuario (es el deliverable). No la parafrasees ni la
"interpretes" salvo que el usuario pida un análisis.

## Qué reporta (todo verificado en vivo)

- **HOOKS**: total cableados en `~/.claude/settings.json` + desglose por evento.
- **MCP servers**: los cableados globalmente (aris4u + sus 5 tools).
- **GUARDS**: guards activos (type-hints, docker-latest, supabase-rls, phi_guard, etc.).
- **MEMORIA**: conteos de `data/sessions.db` (read-only) + desglose por cliente.
- **MODELO por defecto**: `settings.model` (si dice "no fijado", arranca en el default caro).
- **ÚLTIMOS EVENTOS**: tail de `logs/v16.1-events.jsonl`.

## Seguridad

`sessions.db` se abre SIEMPRE con `file:...?mode=ro` (uri=True): el panel no puede
escribir ni corromper la DB (lección del audit V2). Si el panel reporta algo raro,
es lectura — nunca muta estado.

## Relación con el wrapper

Este script es el motor reutilizable de la Capa 0. La Capa 1 (tmux) y la Capa 2
(Electron) consumirían su salida `--json` para pintar un panel gráfico. El doc
maestro de ARIS4U es `~/projects/aris4u/architecture/ARIS4U_MASTER.md`.
