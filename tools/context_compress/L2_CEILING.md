# L2 Cache Relocation Ceiling Analysis
# Fecha: 2026-07-05 | Fuente: 20 sesiones más recientes (de 104 total) | 4,479 mensajes assistant

## VEREDICTO: NO-GO
**L2 ceiling: 0.02% del gasto input equivalente.** Umbral GO = 15-20%. Ratio de miss: 750x.

---

## 1. Economía de cache (20/104 sesiones, 4,479 msgs assistant con usage)

| Métrica | Valor |
|---------|-------|
| input_uncached (precio 1.0x) | 1,964,495 tokens |
| cache_creation (precio 1.25x) | 55,216,809 tokens |
| cache_read (precio 0.1x) | 1,284,106,945 tokens |
| raw_total | 1,341,288,249 tokens |
| **cache hit-rate** | **95.74%** (cache_read / raw_total) |
| cost_equiv total | 199,396,201 unidades |
| **% uncached del gasto** | **0.99%** — superficie de ataque de L2 |
| avg uncached/msg | 439 tokens |

El caching ya captura el 95.74% de los tokens crudos. Solo el 0.99% del gasto equivalente
es input sin cachear.

---

## 2. Huella de inyecciones ARIS4U

### Hooks UserPromptSubmit activos
- `dispatch.py` (ARIS4U) — ROUTING + LOCKED + RECALL + ORQUESTA
- `quota-governor.py` — estado de cuota semanal/5h
- `governor-context.py` — estado de RAM y concurrencia
- `enterprise-build-hint.py` — condicional (no activa en la mayoría de prompts)
- `clarify-gate.py` — condicional (no activa en la mayoría de prompts)

### Tamaño por turno (medido empíricamente)
| Sección | chars | tokens | Estabilidad |
|---------|-------|--------|-------------|
| ROUTING hint | 332 | ~83 | 93-100% estable entre intents |
| LOCKED contexts | 224 | ~56 | 70% estable (depende de sesión) |
| RECALL snippets | ~745 | ~186 | ~50% estable (búsqueda semántica, volátil) |
| ORQUESTA toolkit | ~943 | ~235 | 95-100% estable dentro del mismo intent |
| quota-governor | ~116 | ~29 | 30% (números reales cambian c/5 min) |
| governor-context | ~168 | ~42 | 55% (RAM/concurrencia cambian) |
| **Total inyección/turno** | **~2528** | **~631** | **41% estable (~260 tok)** |

### Fracción estable vs volátil
- Estable/relocalizable: ~260 tokens/turno (41%)
- Volátil (RECALL + valores numéricos dinámicos): ~371 tokens/turno (59%)

Nota: la sección RECALL es la fracción más grande (~186 tok) y es por definición
no-relocalizable: su valor reside en ser context-specific cada turno.

---

## 3. Techo L2 estimado

### Cálculo en cascada

```
Turnos humanos (typed) en 20 sesiones: 208 de 4,479 total = 4.6%
  (el 95.4% restante son tool results y respuestas agénticas sin hook injection)

Tokens inyectados totales: 208 turnos × 631 tok = 131,248 tokens
Cobertura sobre uncached total: 131,248 / 1,964,495 = 6.7%

Tokens relocalizables (41% estables): 208 × 260 = 54,080 tokens
Ahorro si relocalizados (uncached→cache_read): 54,080 × (1.0 - 0.1) = 48,672 unidades

Ahorro como % del gasto total: 48,672 / 199,396,201 = 0.024%
Incluso si TODA la inyección fuera estable (upper bound): 131,248 × 0.9 / 199,396,201 = 0.059%
```

### Factores multiplicativos que colapsan el techo
1. Hit-rate ya 95.74% → costo uncached es solo 0.99% del gasto total
2. Inyecciones cubren solo el 6.7% del uncached (el resto = texto humano + tool results)
3. Solo el 41% de la inyección es estable (RECALL y valores dinámicos no aplican)
4. Turnos humanos = solo 4.6% de todos los turns (sesiones agénticamente intensivas)

Fórmula compacta:
`L2_max = 0.0099 × 0.067 × 0.41 × 100 = 0.027%`

---

## 4. Veredicto: NO-GO

**Ceiling medido: 0.024% del gasto input-equivalente.**
**Upper bound teórico: 0.06%.**
**Umbral mínimo GO: 15-20%.**

La brecha es de ~750x. L2 no es marginal — es irrelevante en este contexto.

**Causa raíz**: el caching existente ya captura el 95.74% de los tokens. La superficie
uncached restante (0.99% del gasto) es en su mayoría contenido irreduciblemente nuevo
(texto humano del turno actual, tool results agénticos, snippets de RECALL específicos
del contexto). La fracción que L2 podría reclamar es microscópica.

**La hipótesis "96% hit-rate → L2 es marginal" se confirma empíricamente.**

---

## Datos de sesiones (top 10 por volumen)

| SID | n_asst | uncached | cache_read | hit% | uncached%cost |
|-----|--------|----------|------------|------|---------------|
| 5b108364 | 1292 | 471,753 | 634,803,833 | 97% | 1% |
| bf5b4018 | 1023 | 138,428 | 342,562,820 | 96% | 0% |
| e5154db8 | 370 | 442 | 36,886,411 | 97% | 0% |
| 9790e59f | 253 | 377 | 25,448,833 | 94% | 0% |
| 2788e4cd | 247 | 180,796 | 42,420,460 | 96% | 3% |
| 1924e7c1 | 226 | 248,901 | 42,971,108 | 91% | 3% |
| 106aebb2 | 196 | 114,396 | 33,708,613 | 93% | 2% |
| cd10b2e4 | 181 | 301 | 17,281,470 | 91% | 0% |
| c32eafbe | 158 | 7,304 | 15,208,023 | 95% | 0% |
| 7fe28cdd | 147 | 145,709 | 27,248,772 | 94% | 3% |

---

## Qué SÍ vale optimizar (si se busca reducir costos)

El 95.74% del gasto viene de `cache_creation` (1.25x precio), no de uncached.
Los `cache_creation` son la escritura del contexto creciente de sesión al cache —
cada vuelta de un hilo gordo paga cache_creation aunque el hit-rate sea alto.

La palanca real ya identificada en model-governance.md H3:
**Delegar volumen (leer archivos, exploración) a subagentes Sonnet para que el hilo Opus
no crezca.** Cada token adicional en el hilo top multiplica cache_creation en cada turno.

L2 no es la palanca. La palanca es el tamaño del hilo.
