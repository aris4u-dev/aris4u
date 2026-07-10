# REGLA DE ORO #1: PARALELISMO CON DISCIPLINA DE HARDWARE Y DE COSTO — Template
# Instanciar en: ~/.claude/rules/parallel-dispatch.md
# Ver hardware.md para los valores de RAM/GPU/cores de tu instalación.

## MODO POR DEFECTO DE CADA SESIÓN CLI

La sesión donde el usuario escribe es, por defecto, ORQUESTADORA. Tres deberes permanentes:

1. **Orquesta, no ejecutes a mano lo pesado.** Hilo principal = decidir + despachar Agent()/Workflow en
   paralelo para trabajo independiente; recibir digests compactos, nunca output crudo en el contexto.

2. **Balance de hardware GLOBAL y CONTINUO.** Antes de CADA lanzamiento pesado, medir lo que consume TODO
   el sistema: esta sesión + otras sesiones Claude + Docker + Ollama + servicios remotos — no solo el conteo
   de agentes. Salud real = **swap/compressor = 0**, NO "GB libres" (en macOS los GB libres son artefacto RSS).
   Ver `hardware.md` para capacidad medida de tu instalación.

3. **No dejar NADA abandonado.** Al cerrar cada bloque: cosechar journals de workflow, matar huérfanos
   (pytest / ollama runners / npm), detectar sesiones Claude colgadas. NUNCA matar a ciegas — distinguir
   por PPID/uptime y confirmar antes de matar. Un workflow a la vez.

---

## Realidad del hardware

Specs + reglas de uso → **fuente única `~/.claude/rules/hardware.md`**. No duplicar aquí.
RAM segura máxima: **{{RAM_SAFE_GB}} GB** (de {{RAM_GB}} GB total · swap=0 como indicador de salud).
GPU segura máxima: **{{GPU_SAFE_GB}} GB** (≈75% del pool unified en Apple Silicon).

---

## Cómo paralelizar (orden de preferencia)

1. **Workflow tool** — fan-out determinista (pipeline/parallel) para audit, review, research, migraciones.
2. **Varios Agent() en un solo mensaje** — subtareas independientes a la vez.
3. **SSH a worker remoto en background** — solo para trabajo que pertenece a su entorno (CUDA/builds Linux).

---

## Checklist antes de responder

1. ¿2+ subtareas independientes? → paralelo (un mensaje con varios tool calls, o un Workflow).
2. ¿Research + implementación? → research en paralelo PRIMERO.
3. ¿Trabajo pesado (muchos archivos, review)? → Workflow, no inline.

---

## DISCIPLINA ANTI-SATURACIÓN — medir en tu hardware (no asumir)

Números a calibrar empíricamente en tu instalación (`~/.claude/bin/ram-report.sh`):

- **Agentes de Claude = NUBE** (corren en Anthropic; localmente solo el contexto ~0.3GB c/u).
  Lanza hasta `min(16, {{CPU_CORES}}-2)` = **{{MAX_CLOUD_AGENTS}}** agentes de razonamiento sin miedo.
  NO te auto-limites a 4-6.

- **El límite real NO es el conteo de agentes** — es lo que sus HERRAMIENTAS corren local:
  cada agente cargando su propio modelo Ollama (GBs), builds, pytest. Un modelo compartido, no uno por agente.

- **Techo RAM total = {{RAM_GB}} GB** · saturación (swap empieza) ≈ {{RAM_SATURATE_GB}} GB usados.
  Presupuesto seguro: ≤{{RAM_SAFE_GB}} GB.

- **Techo GPU/{{GPU_BACKEND}} = ~{{GPU_SAFE_GB}} GB** (75% de RAM en Apple Silicon).
  Dos modelos grandes chocan aquí ANTES que la RAM total.

- Verifica en vivo antes de fan-out pesado:
  ```bash
  vm_stat | grep -E "Pages (free|active|inactive|wired|compressor)"
  ps -axo pid,comm,rss,etime | sort -k3 -rn | head -10
  # Guard mecánico: ~/.claude/hooks/ram-saturation-guard.sh (bloquea si swap>1GB / disponible<8GB / ≥2 workflows)
  ```

- **NO acumules workflows** (uno a la vez). Cosecha journals y mata huérfanos al terminar.

---

## ROUTING DE MODELOS

Fuente única = `~/.claude/rules/model-governance.md`. Resumen mínimo:
- TODO `Agent()`/`agent()` lleva `model=`:
  síntesis/veredicto → `{{REASONING_MODEL}}` · grueso → `{{WORKER_MODEL}}` · trivial → `{{CHEAP_MODEL}}`
  · gate irreversible → `{{BEST_MODEL}}` (puntual).
- Guard BLOQUEANTE: `~/.claude/hooks/model-routing-guard.py`.
- Motor: `aris4u/tools/model_router.py:route_model()`.

---

## Worker remoto (background, sin bloquear)

```bash
ssh {{WORKER_HOST}} 'comando-pesado' &   # solo si {{WORKER_HOST}} responde y tiene RAM libre
# trabajo local / Agent() en paralelo
wait
```

Verificar antes: `ssh {{WORKER_HOST}} free -h` — no asumir que está libre.

---

## NO hacer

- Secuencial cuando hay independencia.
- Despachar a hosts muertos ({{DEAD_HOSTS}}).
- Saturar el orquestador con muchos workflows/agentes a la vez.
- Olvidar matar huérfanos al cerrar un bloque.
