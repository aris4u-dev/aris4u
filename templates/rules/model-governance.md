# GOBIERNO DE MODELOS — Template (rellenar con valores reales de tu cuenta)
# Placeholders: reemplazar {{...}} con los modelos/precios vigentes en tu cuenta Claude.
# Fuente autoritativa de precios: platform.claude.com/docs/en/about-claude/pricing
# Instanciar en: ~/.claude/rules/model-governance.md

## PRINCIPIO CENTRAL (no cambiar sin evidencia medida)

El gasto de una sesión Claude lo domina el HILO de sesión (el contexto que crece con cada turno),
no el número de subagentes. Medir con `~/.claude/bin/model-discipline-report.py` para conocer
tu ratio real antes de optimizar.

---

## 1. HILO DE SESIÓN — la palanca principal

**Modelo del hilo = {{SESSION_MODEL}}** (p.ej. `claude-opus-4-8` · `claude-sonnet-5` · `claude-haiku-4-5`)
Decisión: escoger el mejor balance costo/calidad para tu ritmo de trabajo y presupuesto.

Reglas clave:
- **H1.** El hilo corre en {{SESSION_MODEL}} todo el tiempo. El VOLUMEN (leer archivos, suites, exploración)
  se delega a subagentes {{SUBAGENT_DEFAULT}} para no inflar el contexto del hilo con lectura cruda.
- **H2.** Máximo {{MAX_EXPENSIVE_SESSIONS}} sesiones del modelo más caro simultáneamente.
- **H3.** Hilo <{{CONTEXT_THRESHOLD_K}}k de contexto: volumen (leer archivos, suites) → subagentes {{SUBAGENT_DEFAULT}}.
- **H4. Gate irreversible:** antes de acción grande/irreversible (versión, migración, borrado, rumbo) →
  `Agent(model="{{GATE_MODEL}}")` puntual → veredicto → ejecutar.

---

## 2. SUBAGENTES — tabla cognitiva

| Modelo | Cuándo usarlo | Ejemplos |
|--------|---------------|---------|
| **{{BEST_MODEL}}** (más caro) | SOLO gate puntual (H4): decisiones irreversibles, plan maestro, síntesis de N fuentes | Rediseño de arquitectura, auditoría que sintetiza N hallazgos, bifurcación de rumbo |
| **{{REASONING_MODEL}}** | Síntesis/veredicto/juicio multi-fuente, revisión adversarial, coding correctness difícil | El bug que {{WORKER_MODEL}} no halló; SWE-bench-style correctness |
| **{{WORKER_MODEL}}** | DEFAULT de subagentes: fetch/research/lectura de volumen, fixes, edición, deploys, ops, CRUD, docs | La mayoría de los Agent() del día |
| **{{CHEAP_MODEL}}** | Trivial: classify/format/count/label/extract — nunca como hilo ni como worker de juicio | — |

**REGLA DURA:** TODO `Agent()` / `Task` lleva `model=` explícito, O un `subagent_type` cuyo frontmatter ya fija modelo.
Guard mecánico: `~/.claude/hooks/model-routing-guard.py` bloquea (exit 2) si falta `model=`.

**Escalada en DOS peldaños** (antes de saltar al modelo más caro):
1. Subir `effort` del mismo modelo (e.g. `xhigh`, `max`) — ~5x más barato que escalar al modelo superior
2. Solo si aún falla → escalar al modelo superior

---

## 3. ROUTING SEMÁNTICO

```
síntesis/veredicto/juicio-multi-fuente/audit → {{REASONING_MODEL}}
grueso (verify/search/explore/review/read/edit/implement/research) → {{WORKER_MODEL}}
trivial (classify/format/count/label/extract) → {{CHEAP_MODEL}}
gate irreversible único → {{BEST_MODEL}} (puntual, nunca fan-out)
```

Motor: `aris4u/tools/model_router.py:route_model()` — usa alias genéricos (`sonnet`/`opus`/`haiku`/`fable`)
que se resuelven a los model IDs reales. Cambiar los IDs en un solo lugar.

---

## 4. ENFORCEMENT MECÁNICO

- `~/.claude/hooks/model-routing-guard.py` — BLOQUEANTE exit 2 (Agent/Task sin modelo resoluble)
- `~/.claude/hooks/session-tier-reminder.py` — SessionStart: recuerda el costo del hilo actual
- Statusline: hilo caro se muestra en ROJO con 💸 (visible toda la sesión)
- Medición semanal: `python3 ~/.claude/bin/model-discipline-report.py` (baseline del usuario → target <25% gasto en hilo)

---

## 5. PRICING REFERENCE (verificar en platform.claude.com — NO recitar de memoria)

| Modelo | Precio INPUT/MTok | Precio OUTPUT/MTok | Notas |
|--------|------------------|--------------------|-------|
| {{BEST_MODEL}} | ${{BEST_MODEL_INPUT}} | ${{BEST_MODEL_OUTPUT}} | más caro; SOLO gate puntual |
| {{REASONING_MODEL}} | ${{REASONING_MODEL_INPUT}} | ${{REASONING_MODEL_OUTPUT}} | síntesis/veredicto |
| {{WORKER_MODEL}} | ${{WORKER_MODEL_INPUT}} | ${{WORKER_MODEL_OUTPUT}} | default subagentes |
| {{CHEAP_MODEL}} | ${{CHEAP_MODEL_INPUT}} | ${{CHEAP_MODEL_OUTPUT}} | trivial |

> Nota tokenizer: modelos nuevos pueden tokenizar ~30% más tokens que modelos anteriores por el mismo texto.
> Recalibrar estimaciones de costo al cambiar de modelo.

---

## 6. SEMÁNTICA DE OVERRIDE

"Esta vez usa X" de un usuario = SOLO esa invocación/tarea. Este default es permanente y solo cambia
si el usuario edita este archivo. Un override puntual NUNCA se vuelve la nueva norma.
