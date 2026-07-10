---
name: preflight
description: >
  Auditoría de recursos del M5 ANTES de lanzar cualquier fan-out pesado (workflow,
  varios Agent(), build/pytest local, carga de modelo Ollama). Mide lo que consume
  TODO el sistema —esta sesión + otras sesiones Claude + Docker + Ollama + servicios—
  y emite veredicto GO / CAUTION / NO-GO según swap/compressor (no "GB libres", que
  mienten en macOS). Use when: (1) antes de un Workflow o de lanzar ≥4 agentes,
  (2) "¿hay recursos / puedo paralelizar?", (3) el M5 se siente lento, (4) al iniciar
  una sesión orquestadora. Ritualiza la regla presession_resource_audit.
---

# /preflight — Auditoría de recursos pre-fan-out

Veredicto GO/CAUTION/NO-GO leyendo la verdad viva del M5. Salud real = **swap y
compressor en 0**, NO "GB libres" (artefacto RSS en macOS, ver memoria
`reference_macos_ram_rss_artifact`).

## Cómo ejecutar

```bash
bash ~/.claude/bin/preflight.sh
```

Sin argumentos. Read-only, no mata nada (para limpiar usa `/harvest`).

## Qué reporta

1. **Memoria** — `vm_stat`: pages free/active/wired, **swap usado**, **compressor**.
   Umbral: swap>1GB o compressor>2GB → CAUTION; swap creciendo → NO-GO.
2. **Sesiones Claude vivas** — `ps -axo pid,ppid,rss,etime,command | grep claude`:
   RSS y antigüedad de cada una (detecta sesiones colgadas por `etime` alto).
3. **Docker** — `docker stats --no-stream`: contenedores y su RAM (supabase/n8n/etc.).
4. **Ollama** — `/api/ps`: modelos cargados AHORA y su VRAM.
5. **Workflows activos** — cuenta journals de workflow en vuelo (regla: 1 a la vez).

## Veredicto

| Señal | GO | CAUTION | NO-GO |
|-------|----|---------|----|
| swap usado | 0 | <1GB | ≥1GB o creciendo |
| compressor | <1GB | 1-2GB | >2GB |
| RAM disponible | >8GB | 4-8GB | <8GB |
| workflows activos | 0-1 | — | ≥2 |

GO = paraleliza sin miedo (hasta ~16 agentes de razonamiento = nube).
CAUTION = limita builds/ollama locales a ≤2; agentes de razonamiento OK.
NO-GO = corre `/harvest` primero, no lances nada pesado.

Canon: `~/.claude/rules/parallel-dispatch.md` §DISCIPLINA ANTI-SATURACIÓN +
`reference_m5_capacity_model`.
