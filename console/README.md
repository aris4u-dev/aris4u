# ARIS4U Live Console

**Darle vida a ARIS4U:** un panel local-first donde ver el estado VIVO de ARIS4U (generado del
código, nunca stale), navegar cada componente, y —en fases siguientes— operarlo y editarlo con
gobernanza. La consola de **un** sistema que se conoce a sí mismo, no un orquestador genérico.

> Diseño completo: `~/Desktop/HANDOVER-ARIS4U-LIVE-CONSOLE-2026-06-19.md`.

## Principio rector: DESACOPLE TOTAL
Proyecto git **aparte** de `~/projects/aris4u`. Si la Console se rompe, **el núcleo de ARIS4U
sigue funcionando** vía Claude Code normal. Nada irreversible sin git/backup. Read-only sobre el
engine en las Fases 0–2 (sin tocarlo → compatibles con el freeze de ARIS4U).

## Fuentes de verdad (no reinventar)
- **Generador vivo real:** `~/.claude/bin/status-gen.py` (introspección git/db/tests con `proof`).
  La Console reusa su patrón, **no lo reemplaza**.
- **Esquema de componente:** el de `aris_inv.json` (id/name/family/role/maturity/signals).
- **Molde / gold INTOCABLE:** `~/projects/aris4u/architecture/ARIS4U-REPORTE-COMPLETO.html`
  (curado a mano — "más verdades que los logs"). La Console genera su equivalente FRESCO; **no lo
  sobrescribe**.

## Fases (handover §9)
- **Fase 0 — CRM base (inventario auto-generado).** ← EN CURSO
  - [x] **Generador de inventario vivo** (`aris4u_console/inventory.py`): descubre los componentes
        de ARIS4U leyendo el código (un hook/tool/guard/MCP nuevo aparece solo) + señal viva
        (LOC, has_test, último commit, madurez derivada). Salida `out/inventory.json`.
  - [ ] Backend localhost (FastAPI, bind 127.0.0.1) que sirva el inventario.
  - [ ] Menú por componente (frontend) + explicaciones en lenguaje sencillo (LLM-pass cacheado).
- **Fase 1 — Lectura viva:** click en componente → código (Monaco read-only) + estado + memoria.
- **Fase 2 — Terminales:** xterm.js + PTY (modelo local MLX + CLI nativa de Claude).
- **Fase 3 — Edición gobernada** (requiere decisión de freeze): commit → MLX-critica → guards → tests.
- **Fase 4 — Loop cognitivo:** replay de transcripts indexado a memoria + anti-regresión.

## Uso
```bash
# Servidor local: regenera todo + sirve la consola con CÓDIGO VIVO (clic en pieza → ver código)
python3 -m aris4u_console.server        # abre http://127.0.0.1:8787 (read-only, bind 127.0.0.1)

# Solo regenerar la pantalla estática (sin servidor; el clic→código no funciona en modo archivo)
python3 -m aris4u_console.inventory      # out/inventory.json + resumen
python3 -m aris4u_console.render_console # out/console.html

# Tests
python3 -m pytest tests/ -q
```
