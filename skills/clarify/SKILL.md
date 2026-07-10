---
name: clarify
description: >
  Fase de CLARIFICACIÓN dirigida que corre ANTES de planear o construir cualquier feature/módulo/app.
  Escanea un brief o idea cruda con una taxonomía de 11 categorías, detecta las ambigüedades y los
  "desconocidos-desconocidos" que el brief NO especifica, y los resuelve con preguntas UNA a la vez
  (vía AskUserQuestion, con opción recomendada). Devuelve un brief enriquecido + supuestos resueltos,
  listo para PLAN. Use when: (1) el usuario describe una feature/app/módulo sin detallar todas las
  reglas, (2) ANTES de enterprise-build / ui-plan-review / cualquier fase de diseño, (3) "aclaremos
  esto antes de construir", (4) un brief que huele ambiguo. NO usar para tareas mecánicas triviales
  ni cuando el brief ya está completamente especificado.
version: 0.1.0
maintainer: aris4u
category: planning
tags: [spec, clarify, planning, requirements, pre-plan]
effort: medium
---

# Clarify — clarificación dirigida antes de planear

## Por qué existe

El error caro no es construir mal: es **planear sobre suposiciones que el brief nunca dijo**.
Un checklist rígido solo verifica lo que YA sabes; no caza lo que falta. Esta fase mueve las
preguntas **antes** del plan: detecta las ambigüedades reales del brief y las resuelve con el
humano, una a la vez, para que el plan se construya sobre intención explícita y no sobre relleno.

> Antídoto al patrón observado: el andamiaje (checklist/contrato) estrecha el razonamiento si
> llega antes de aclarar el QUÉ. Clarify se ejecuta entre el prompt crudo y la fase PLAN.

Reconstruido del concepto `/speckit.clarify` de GitHub Spec Kit (MIT). NO usa su CLI ni sus
archivos; es un prompt nuestro.

## Cuándo dispara

- Antes de `enterprise-build`, `ui-plan-review`, `ui-discover` o cualquier diseño de feature.
- Cuando el usuario describe "un CRM/app/módulo para X" sin fijar reglas, métricas o límites.
- Cuando aparece un brief con verbos vagos ("mejóralo", "que sea rápido", "moderno").

**NO dispares** si el brief ya está completamente especificado, o para tareas mecánicas (un rename,
un fix puntual). Si tras el escaneo no hay ambigüedades materiales, dilo y pasa directo a PLAN.

## Cómo funciona (flujo)

### 1. Escaneo con la taxonomía de 11 categorías

Lee el brief y, para CADA categoría, marca internamente lo que falta como
`[FALTA: <qué no se especificó>]`. No preguntes todavía: primero arma la cola completa.

| # | Categoría | Qué buscas que falte |
|---|-----------|----------------------|
| 1 | **Alcance funcional y comportamiento** | qué hace exactamente; qué NO hace; flujos principales |
| 2 | **Modelo de datos y entidades** | entidades, relaciones, identidad, ciclo de vida, fuente de verdad |
| 3 | **UX y flujo de interacción** | quién lo usa, pantallas/pasos, estados vacíos/error, navegación |
| 4 | **Requisitos no funcionales** | volumen, latencia, concurrencia, disponibilidad, offline |
| 5 | **Integraciones y dependencias externas** | APIs, servicios, auth de terceros, webhooks, qué ya existe |
| 6 | **Casos borde y manejo de fallos** | qué pasa cuando falla/duplica/expira; idempotencia; reintentos |
| 7 | **Restricciones y trade-offs** | presupuesto, plazo, stack obligado, lo que se sacrifica |
| 8 | **Terminología y definiciones** | términos del dominio con significado preciso (evitar ambigüedad) |
| 9 | **Criterios de aceptación y métricas de éxito** | cómo se mide "hecho"; número objetivo; señal de valor |
| 10 | **Seguridad, privacidad y compliance** | datos sensibles/PHI, RBAC, RLS, retención, auditoría |
| 11 | **Supuestos y fuera-de-alcance** | qué das por sentado; qué queda explícitamente fuera |

### 2. Prioriza y pregunta UNA a la vez

- Ordena las lagunas por **impacto en el plan** (lo que cambiaría la arquitectura va primero).
- Pregunta **de una en una** con **`AskUserQuestion`**: enunciado claro + 2-4 opciones, cada una
  con su valor/implicación en la descripción, y marca la **recomendada** primera con "(Recomendada)".
- Tras cada respuesta, registra la decisión y sigue con la siguiente laguna.
- **Tope:** máximo ~7 preguntas. Si quedan lagunas menores, conviértelas en supuestos explícitos
  (categoría 11) y decláralos, no preguntes todo.
- NO listes preguntas en prosa esperando respuesta en texto. Siempre `AskUserQuestion`.

### 3. Devuelve el brief enriquecido

Al cerrar, produce:

```
## Brief clarificado
<el brief original reescrito con las respuestas integradas>

## Decisiones resueltas
- [cat N] <pregunta> → <respuesta elegida>

## Supuestos declarados (no preguntados)
- <supuesto> (categoría N) — revertir si es falso

## Fuera de alcance
- <lo que queda explícitamente fuera>

## Listo para PLAN
<1 línea: qué construir, con qué criterio de éxito medible>
```

## Reglas

- Una pregunta a la vez, siempre vía `AskUserQuestion` (regla de la casa).
- No inventes requisitos: si algo no se dijo y no es crítico, va como supuesto declarado.
- Si el brief toca datos sensibles/PHI, la categoría 10 es obligatoria, no opcional.
- Si tras el escaneo no hay ambigüedad material: dilo y pasa a PLAN sin preguntar.
- No construyas nada aquí. Esta fase termina en el brief enriquecido; el build es después.

## Encaja con

- **Antes de:** `enterprise-build`, `aris4u-ui-pipeline:ui-plan-review`, `ui-discover`.
- **Complementa:** `ui-discover` descubre el dominio; `clarify` resuelve las ambigüedades del brief
  concreto. Distinto eje, mismo objetivo: que el plan no hardcodee suposiciones.
