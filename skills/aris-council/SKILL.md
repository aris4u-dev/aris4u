---
name: aris-council
description: >
  Consejo de razonamiento ARIS4U: somete una DECISIÓN o pregunta difícil a 5 lentes de
  razonamiento DISTINTOS (contrarian, first-principles, expansionist, outsider, executor),
  ejecutados como sub-agentes independientes y ciegos entre sí, y luego sintetiza en un
  veredicto con convergencias, conflictos y recomendación honesta. Diversidad de MÉTODO,
  no de modelo (research DMAD/ICLR 2025). Razonamiento fuerte (Opus sintetiza), NO el
  modelo local débil de aris_dialectic. Use when: (1) decisión de arquitectura/estrategia
  con trade-offs, (2) "dame perspectivas", "qué se me escapa", "pásalo por el consejo",
  (3) elección entre alternativas, evaluación de riesgo, debugging de un razonamiento,
  (4) antes de comprometer una decisión importante. NO para código (eso es aris_dialectic)
  ni para datos PHI que no pueden salir a la API (eso es aris_dialectic mode local).
---

# /aris-council — Consejo de razonamiento (5 lentes)

Somete una decisión difícil a **cinco métodos de razonamiento distintos**, ejecutados de
forma **independiente**, y entrega un veredicto sintetizado. El valor está en la diversidad
de *método* (no de modelo): cada lente ataca el problema de una forma que las otras no ven.

**Cuándo NO usar esto:**
- Revisión de **código** → usa `aris_dialectic` (Builder/Reviewer/Security).
- Decisión con **datos PHI** que no pueden ir a la API de Claude → usa el camino local
  (`aris_dialectic`), no esta skill.
- Pregunta trivial o de un solo hecho → respóndela directo; el consejo es overhead.

## Entrada

La decisión/pregunta va en los argumentos (`$ARGUMENTS`). Si viene vaga, pide
**una** aclaración antes de convocar el consejo (no malgastes 5 agentes en algo mal definido).

## Protocolo

### 1. Enmarca la decisión (hilo principal, Opus)
- Reformula la decisión en 1-3 frases cristalinas: qué se decide, qué opciones hay, qué
  está en juego.
- **Recall de contexto**: corre `aris_search` (y `aris_recall_client` si la decisión es de
  un cliente concreto) para traer decisiones previas/guards relevantes. Si hay una decisión
  bloqueada que contradice esto, sácala a la luz ANTES del consejo.

### 2. Convoca los 5 lentes — sub-agentes INDEPENDIENTES y CIEGOS
Lanza **5 sub-agentes en paralelo** (un solo mensaje, varios `Agent()`), `subagent_type:
general-purpose`, **`model: sonnet`** (mecánico-paralelo; la síntesis fuerte la hace Opus
en el hilo principal). Cada agente recibe SOLO la decisión enmarcada + SU lente — **nunca**
el output de los otros (independencia real = anti-groupthink, evita la tiranía de la mayoría).

Cada agente devuelve: su análisis (≤200 palabras), su hallazgo más fuerte, y un nivel de
confianza (alto/medio/bajo).

Los 5 lentes (prompt de cada uno):

1. **Contrarian (inversión)** — "Asume que esta decisión YA FALLÓ de la peor forma posible.
   Trabaja hacia atrás: ¿cuál fue el modo de fallo? ¿Qué señal temprana se ignoró? ¿Qué
   supuesto resultó falso? No critiques en abstracto: describe el camino concreto al desastre."

2. **First-principles (descomposición)** — "Descompón esta decisión en sus claims atómicos.
   Para cada uno marca: ¿es un HECHO verificado o un SUPUESTO heredado? Reconstruye la
   conclusión solo desde los hechos. ¿Sobrevive? ¿Qué supuesto, si cae, derrumba todo?"

3. **Expansionist (analogía)** — "¿Qué problema ANÁLOGO en otro dominio (otra industria,
   la naturaleza, la historia, otro stack) ya está resuelto? Mapea ese patrón a esta
   decisión. ¿Qué solución probada estamos reinventando o ignorando?"

4. **Outsider (pregunta ingenua)** — "Asume CERO contexto previo. Eres alguien que llega
   hoy. ¿Qué 'obviedad' que todos dan por sentada cuestionarías? ¿Qué pregunta tonta nadie
   se atreve a hacer? ¿Por qué esto siquiera es un problema?"

5. **Executor (grafo de dependencias)** — "Ignora si la decisión es 'correcta'. ¿Es
   EJECUTABLE? Mapea la secuencia: qué tiene que pasar primero, qué depende de qué, dónde
   está el critical path y el cuello de botella real. ¿Qué bloquea todo lo demás?"

**Modo quick (opcional):** si la decisión es de menor calado, convoca solo 3 —
**contrarian, first-principles, executor** (cubren riesgo, validez y factibilidad).

### 3. Sintetiza (hilo principal, Opus) — esto es lo que importa
No pegues los 5 outputs. **Sintetiza:**
- **Convergencia** — dónde coinciden ≥3 lentes = señal de alta confianza.
- **Conflicto** — dónde chocan = AHÍ está el riesgo real; no lo promedies, examínalo.
- **Lo decisivo** — el supuesto o dependencia que, si cae, cambia la respuesta.
- **Recomendación** — clara, con nivel de confianza y **qué evidencia la cambiaría**.
- **Honestidad** — si los 5 lentes son superficiales o el consejo no aportó sobre tu
  análisis directo, DILO. El consejo no siempre gana; admítelo cuando sea teatro.

### 4. (Opcional) Persiste
Si la decisión es importante y se compromete, ofrece guardarla con `aris_ingest`
(scoped al cliente si aplica) para que quede como decisión recuperable.

## Notas de diseño
- **Por qué Sonnet en los lentes y Opus en síntesis:** aplicar UN lente es trabajo acotado
  (Sonnet sobra); la síntesis —reconciliar conflictos y decidir— es lo cognitivamente duro
  (Opus). Alinea con el routing de modelos de ARIS4U.
- **Por qué independientes/ciegos:** el research (DMAD ICLR 2025; fallos de multi-agent
  debate) muestra que la deliberación secuencial sufre *problem drift* y *tiranía de la
  mayoría*. Generación independiente + síntesis única lo evita.
- **Diversidad de método > de modelo:** Self-MoA mostró que mezclar modelos baja la calidad;
  el valor está en los 5 *métodos*, no en 5 *modelos* distintos.
