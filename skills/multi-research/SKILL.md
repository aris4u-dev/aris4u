---
name: multi-research
description: >
  Investigacion cruzada multi-modelo usando APIs directas. Lanza consultas paralelas a Claude + Gemini + Grok via SSH a W2,
  luego sintetiza con Claude. Ideal para decisiones tecnicas, evaluacion de herramientas, y analisis profundo.
  Use when: (1) Evaluar una herramienta/framework/libreria, (2) Tomar una decision tecnica con multiples perspectivas,
  (3) Analizar un tema complejo que necesita triangulacion, (4) Comparar opciones antes de implementar.
version: 0.2.0
maintainer: aris4u
category: research
tags: [research, multi-model, analysis, decision-making]
dependencies:
  tools: [curl, jq, claude-dispatch]
effort: high
---

# Multi-Research — Analisis Cruzado Multi-Modelo

## Overview

Skill que lanza la misma pregunta a 3 modelos pagados en paralelo via APIs directas,
recoge las respuestas, y Claude sintetiza un veredicto final.

**NOTA**: Este skill usa APIs directas (no LiteLLM). Para research profundo con formato
de reporte completo, usar `/deep-research` (dev:deep-research).

## Cuando Usar

- Decisiones tecnicas importantes (que framework, que approach)
- Evaluar si algo es real o hype
- Investigar un tema donde un solo modelo puede tener bias
- Antes de implementar algo complejo

## Workflow

### Paso 1: Recibir la pregunta del usuario

El usuario da un tema/pregunta. Puede ser:
- "Evalua si Deno es mejor que Node para nuestro caso"
- "Que base de datos usar para X"
- "Es real lo que dice este articulo sobre Y"

### Paso 2: Lanzar 3 queries en paralelo a W2

Usar el Bash tool para lanzar los 3 en paralelo (un solo mensaje, 3 tool calls).
TODAS las ejecuciones van a W2 via SSH. Mac = orquestador puro.

**Modelo 1** — Claude (deep analysis):
```bash
ssh w2 'claude-dispatch --bare --print "PREGUNTA — analiza en profundidad, max 500 palabras"' > /tmp/research_claude.txt 2>&1
```

**Modelo 2** — Gemini 2.0 Flash (web-connected, analitico):
```bash
ssh w2 'source ~/CLAUDE/.env.apikeys && curl -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=$GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"contents\":[{\"parts\":[{\"text\":\"PREGUNTA — max 500 palabras, incluye datos recientes\"}]}]}" \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print(r[\"candidates\"][0][\"content\"][\"parts\"][0][\"text\"])"' > /tmp/research_gemini.txt 2>&1
```

**Modelo 3** — Grok (perspectiva alternativa/contrarian):
```bash
ssh w2 'source ~/CLAUDE/.env.apikeys && curl -s "https://api.x.ai/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -d "{\"model\":\"grok-4-1-fast\",\"messages\":[{\"role\":\"user\",\"content\":\"PREGUNTA — perspectiva critica, max 500 palabras\"}]}" \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print(r[\"choices\"][0][\"message\"][\"content\"])"' > /tmp/research_grok.txt 2>&1
```

### Paso 3: Claude sintetiza

Con las 3 respuestas, Claude produce:

1. **Consenso**: En que coinciden los 3 modelos
2. **Divergencias**: Donde difieren y por que
3. **Veredicto**: Recomendacion final con nivel de confianza (alto/medio/bajo)
4. **Accion**: Que hacer concretamente

## Reglas

- NUNCA enviar datos sensibles/PHI a modelos externos — solo Ollama local
- Si un modelo falla, continuar con los otros 2 (graceful degradation)
- Todo se ejecuta en W2 via SSH — Mac no ejecuta nada
- Formato de salida: tabla comparativa + veredicto claro
- Maximo 500 palabras por modelo para no saturar contexto

## Ejemplo de Uso

```
/multi-research "Debo usar Supabase Edge Functions o Firebase Cloud Functions para mi-proyecto?"
```

Output esperado:
| Aspecto | Claude | Gemini | Grok |
|---------|--------|--------|------|
| Recomendacion | Supabase | Supabase | Firebase |
| Razon | Ya usamos Supabase | Menor latencia | Mejor docs |

**Consenso**: 2/3 recomiendan Supabase
**Veredicto**: Supabase Edge Functions (confianza ALTA)
**Accion**: Implementar con Supabase, ya tenemos la infra

## Chain Triggers (Auto-sugerencias post-ejecucion)

Despues de sintetizar la investigacion, Claude DEBE sugerir:
1. **prd-generator** — Si la investigacion fue para decidir tech stack: "Genero un PRD con esta decision?"
2. **architect-review** — Si se evaluo una herramienta: "Hago un review de como integraria en la arquitectura?"

## Inputs From (Skills que alimentan este)

- **youtube** → Videos tecnicos como fuente adicional de investigacion
- **firecrawl-scraper** → Datos scrapeados de docs/blogs como input
- **market-research** → Contexto de mercado para decisiones tech con impacto de negocio
