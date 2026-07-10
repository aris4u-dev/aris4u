---
name: aris-config
description: >
  Visor (y editor mínimo) de la configuración de ARIS4U: modelo por defecto, env/flags
  (ARIS4U_HEALTHCARE, ARIS4U_AUTOUPDATE, caching, concurrency, autocompact), y MCP servers
  cableados (detecta duplicados global vs .mcp.json del repo). Use when: (1) "config de
  ARIS4U", "qué flags tengo", (2) "cambiar el modelo por defecto", (3) revisar dónde vive
  cada setting, (4) diagnosticar MCP duplicado. Capa 0 del wrapper ARIS4U Desktop.
---

# /aris-config — Configuración de ARIS4U

Muestra toda la configuración efectiva de ARIS4U y dónde vive, para no abrir JSON a mano.

## Cómo ejecutar

```bash
python3 ~/projects/aris4u/tools/aris_config.py            # tabla de config
python3 ~/projects/aris4u/tools/aris_config.py --json     # estructura
```

Imprime la salida al usuario. Reporta env/flags, modelo por defecto, y MCP servers
(marcando duplicados).

## Cambiar el modelo por defecto

Para fijar el modelo con el que arranca Claude Code (ahorro Fable→Opus del handover):

```bash
python3 ~/projects/aris4u/tools/aris_config.py --set-model claude-opus-4-8
```

- Edita `~/.claude/settings.json` y deja backup `settings.json.bak-set-model`.
- Recomendado: `claude-opus-4-8` por defecto; subir a `claude-fable-5` a mano (`/model`)
  solo para bloques de estrategia/arquitectura. Ver el §AHORRO del handover.
- El cambio aplica a la PRÓXIMA sesión (no a la actual).

## Notas

- La única mutación que hace es `--set-model` (con backup). Todo lo demás es lectura.
- Si reporta `aris4u` MCP duplicado (global + repo), conviene dejar una sola fuente.
- Para editar otros flags (ARIS4U_HEALTHCARE, etc.), se editan en
  `~/.claude/settings.json` clave `env` (usar la skill `update-config` o edición directa).
