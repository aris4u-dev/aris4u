# Protocolo de Analisis Avanzado de Videos YouTube
# Para evaluacion de herramientas, frameworks y mejoras al ambiente
# Basado en sesion de Skills Audit 2026-03-04
# Probado con: 3 videos Claude Code Skills → 5 acciones implementadas

## Cuando Usar Este Protocolo

Activar cuando el usuario comparte videos de YouTube sobre:
- Herramientas AI, frameworks, plugins, workflows
- Mejoras a Claude Code, skills, memoria, configuracion
- Nuevas tecnologias para proyectos activos o infraestructura
- Cualquier contenido que podria cambiar como trabajamos

NO usar para: videos informativos generales, tutoriales paso a paso, entretenimiento.

---

## FASE 1: Extraccion y Triage Rapido (5 min por video)

### 1.1 Metadata
Para cada video, extraer:
```bash
yt-dlp --print "%(title)s\n%(channel)s\n%(upload_date)s\n%(duration_string)s\n%(view_count)s views" --no-download "URL"
```

### 1.2 Triage por Metadata (antes de transcribir)
Clasificar cada video con puntuacion de relevancia:

| Criterio | Score | Indicador |
|----------|-------|-----------|
| Canal tecnico conocido | +3 | Canales con historial de contenido tecnico real |
| Titulo con CAPS/hype | -2 | "INSANE!", "MASSIVE!", "GAME CHANGER!" = probable clickbait |
| Duracion 10-25 min | +2 | Rango optimo para contenido tecnico sustancial |
| Duracion < 5 min | -1 | Demasiado corto para profundidad |
| Duracion > 30 min | 0 | Puede ser valioso pero costoso en tokens |
| Fecha < 7 dias | +1 | Contenido fresco, posiblemente relevante |
| Views > 10K en < 7 dias | +1 | Validacion comunitaria |

**Decision:**
- Score >= 3: Transcribir y analizar completo
- Score 1-2: Transcribir, revisar manualmente primero
- Score <= 0: Avisar al usuario que parece hype, pedir confirmacion

### 1.3 Extraccion de Transcripcion
```bash
# Usar el skill youtube standard
./scripts/yt-transcript.sh "URL" en
```

Si falla: fallback a Whisper local.

---

## FASE 2: Clasificacion de Contenido (10 min total)

### 2.1 Lectura Rapida de Claude
Claude lee cada transcripcion y clasifica:

**Categoria del video:**

| Tipo | Descripcion | Ejemplo |
|------|-------------|---------|
| TOOL | Presenta una herramienta/plugin concreto | "Nuevo Skill Creator plugin" |
| ARCH | Explica arquitectura/patrones | "Como estructurar skills" |
| WORKFLOW | Muestra un flujo de trabajo | "Mi proceso de code review" |
| HYPE | Marketing/afiliado/clickbait | "INSANE new feature!!!" |
| TUTORIAL | Paso a paso educativo | "Como instalar X" |
| REVIEW | Comparativa/evaluacion | "X vs Y para productividad" |

**Metrica Hype vs Realidad:**

```
HYPE indicators (restar puntos):
- Adjetivos extremos sin evidencia ("insane", "game changer", "revolutionary")
- Links de afiliado o sponsor mentions
- Demostraciones superficiales sin mostrar limitaciones
- Promesas de resultados sin datos
- "Everyone should do this" sin contexto de quien se beneficia

REALITY indicators (sumar puntos):
- Numeros concretos (ej: "20% activation → 84% with optimization")
- Limitaciones mencionadas explicitamente
- Codigo o configuracion mostrada
- Antes/despues con datos medibles
- Menciona cuando NO usar algo
```

**Formula:**
```
Reality Score = (REALITY indicators) / (REALITY + HYPE indicators) * 100
```

- > 70%: Contenido tecnico solido
- 40-70%: Mezcla — extraer solo los datos concretos
- < 40%: Mayoria hype — extraer maximo 1-2 datos utiles si los hay

### 2.2 Extraccion de Claims Tecnicos

Para cada video con Reality Score > 40%, extraer una lista estructurada:

```
CLAIM: [afirmacion tecnica especifica]
EVIDENCIA: [dato, demo, o codigo que lo respalda]
VERIFICABLE: [SI/NO — podemos verificarlo en nuestro ambiente?]
RELEVANCIA: [ALTA/MEDIA/BAJA para nuestro setup]
```

---

## FASE 3: Cross-Analisis Multi-Modelo (15 min)

### 3.1 Preparar Contexto

Crear dos archivos en /tmp/yt-analysis/:

**our_setup.md** — Estado actual del ambiente:
- Arquitectura (local)
- Sistema de memoria actual (CLAUDE.md, MEMORY.md, docs/)
- Skills actuales y su YAML budget
- Proyectos activos y su estado
- Tiers de modelos y costos
- Cualquier metrica relevante al tema del video

**analysis_prompt.md** — Prompt estructurado:
```markdown
# TASK: Evaluate [TOPIC] recommendations against our current setup

## VIDEO SUMMARIES
[Resumen de cada video con claims tecnicos extraidos]

## CURRENT SETUP
[Contenido de our_setup.md]

## ANALYSIS REQUIRED
1. Hype vs Reality: % util vs marketing por video
2. Gap Analysis: que NO estamos haciendo que DEBERIAMOS
3. Architecture Audit: nuestro setup cumple best practices?
4. Concrete Action Plan: lista priorizada con esfuerzo estimado
5. Risk Assessment: que podria salir mal, que es overkill

Be brutally honest. We have a sophisticated setup — tell us
what's ACTUALLY new and useful vs what we already do.
```

### 3.2 Enviar a 3 Modelos (paralelo via SSH a W2)

Usar APIs directas desde W2 (todas las API keys en ~/CLAUDE/.env.apikeys):

```bash
# Modelo 1: Claude (analisis profundo)
ssh w2 'claude-dispatch --bare --print "PROMPT"' > /tmp/yt-claude.txt &

# Modelo 2: Gemini 2.5 Flash (contexto largo, conocimiento amplio)
ssh w2 'source ~/CLAUDE/.env.apikeys && curl -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=$GEMINI_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"contents\":[{\"parts\":[{\"text\":\"PROMPT\"}]}]}" \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print(r[\"candidates\"][0][\"content\"][\"parts\"][0][\"text\"])"' > /tmp/yt-gemini.txt &

# Modelo 3: Grok (perspectiva critica/contrarian)
ssh w2 'source ~/CLAUDE/.env.apikeys && curl -s "https://api.x.ai/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -d "{\"model\":\"grok-4-1-fast\",\"messages\":[{\"role\":\"user\",\"content\":\"PROMPT\"}]}" \
  | python3 -c "import sys,json; r=json.load(sys.stdin); print(r[\"choices\"][0][\"message\"][\"content\"])"' > /tmp/yt-grok.txt &
wait
```

**Todos los modelos reciben el mismo prompt exacto** para comparar respuestas.

### 3.3 Sintesis de Consenso

Claude (Opus) sintetiza las 3 respuestas buscando:

| Patron | Significado | Accion |
|--------|------------|--------|
| 3/3 coinciden | Consenso fuerte | Implementar con confianza |
| 2/3 coinciden | Consenso parcial | Investigar el punto de divergencia |
| 0/3 coinciden | Sin consenso | Requiere analisis manual de Claude |
| Todos dicen "ya lo haces" | Validacion | No action needed — documentar |
| Todos dicen "gap critico" | Prioridad alta | Implementar inmediatamente |

---

## FASE 4: Auditoria de Nuestro Ambiente (20 min)

### 4.1 Verificacion contra Claims

Para cada claim con RELEVANCIA ALTA y consenso de modelos:

```bash
# Ejemplo: "15,000 character limit for skills YAML"
# Verificar en nuestro ambiente:
find ~/.claude/skills -iname "SKILL.md" -exec python3 -c "
content = open('{}').read()
if content.startswith('---'):
    end = content.find('---', 3)
    print(f'{}: {len(content[3:end])} chars')
" \;
```

**Checklist de auditoria por area:**

| Area | Que verificar | Comando |
|------|--------------|---------|
| Skills YAML budget | Total chars < 15K | Contar YAML frontmatter de todos los skills |
| Progressive disclosure | Skills cargan refs on-demand | Revisar skill.md por "read references/" |
| Memoria duplicada | CLAUDE.md vs MEMORY.md overlap | diff conceptual de ambos archivos |
| Context siempre cargado | Chars totales al inicio | wc -c CLAUDE.md + MEMORY.md |
| Plugins innecesarios | Marketplace skills no usados | du -sh ~/.claude/skills/*/ |
| Post-compact hook | Info actualizada | cat hooks/post-compact-context.sh |

### 4.2 Scoring de Impacto

Para cada mejora propuesta:

```
MEJORA: [descripcion]
ESFUERZO: [minutos estimados]
IMPACTO: [ALTO/MEDIO/BAJO]
RIESGO: [que puede salir mal]
REVERSIBLE: [SI/NO — podemos revertir facilmente?]
PRIORIDAD = IMPACTO / ESFUERZO (ratio)
```

**Ordenar por PRIORIDAD descendente.** Implementar de arriba hacia abajo.

---

## FASE 5: Decision e Implementacion

### 5.1 Matriz de Decision

| Prioridad | Esfuerzo | Accion |
|-----------|----------|--------|
| ALTA + < 30 min | Implementar AHORA en esta sesion |
| ALTA + 30-120 min | Programar para siguiente sesion |
| ALTA + > 2 hrs | Crear tarea en GSD con plan |
| MEDIA | Solo si queda tiempo en la sesion |
| BAJA | Documentar en decisiones.md para futuro |

### 5.2 Protocolo de Implementacion

1. **Antes de cambiar**: Verificar estado actual (git status, backup si es destructivo)
2. **Cambio atomico**: Una mejora a la vez, verificar que funciona
3. **Medir antes/despues**: Ej: "YAML budget ANTES: 63K → DESPUES: 10.4K"
4. **Documentar**: Actualizar session-log.md con cada cambio
5. **Si algo falla**: Revertir inmediatamente, no intentar arreglar sobre lo roto

### 5.3 Que NUNCA Implementar sin Cuestionar

| Red Flag | Por que | Alternativa |
|----------|---------|-------------|
| "Install this marketplace with 200+ skills" | Infla YAML budget, la mayoria no se usa | Evaluar skill por skill, instalar solo los necesarios |
| "Replace your entire memory system" | Alto riesgo, el nuestro ya funciona | Optimizar incrementalmente |
| "Use this paid API for X" | CERO GASTO sin autorización explícita | Buscar alternativa gratis primero |
| "Connect to cloud service Y" | Privacidad, dependencia, costo | Solucion local cuando sea posible |
| "This framework replaces everything" | Over-engineering | Evaluar que parte especifica ayuda |

---

## FASE 6: Reporte final

### Formato de Reporte Final

```
## Analisis de [N] Videos: [TEMA]

### Triage
| Video | Canal | Duracion | Reality Score | Veredicto |
|-------|-------|----------|---------------|-----------|

### Top 3 Hallazgos (consenso multi-modelo)
1. [Hallazgo con datos concretos]
2. [Hallazgo con datos concretos]
3. [Hallazgo con datos concretos]

### Acciones Ejecutadas
| # | Accion | Antes | Despues | Impacto |
|---|--------|-------|---------|---------|

### Acciones Pendientes (requieren decision del usuario)
- [ ] [Accion que implica gasto o riesgo]

### Proximos Pasos Sugeridos
- [Sugerencia 1]
- [Sugerencia 2]
```

---

## Ejemplo Real: Sesion Skills Audit 2026-03-04

**Input**: 3 videos sobre Claude Code Skills
**Fase 1**: Metadata → Video 1 tenia titulo hype (score -1), Videos 2-3 tecnico (score +4)
**Fase 2**: Reality Scores → Video 1: 5%, Video 2: 75%, Video 3: 60%
**Fase 3**: Cross-analisis Grok+Gemini+Groq → consenso en 3 gaps criticos
**Fase 4**: Auditoria revelo 120 marketplace skills = 58K chars (4X sobre limite 15K)
**Fase 5**: 5 acciones implementadas en 45 min:
  1. legal-plugin eliminado (-91 skills, -29K chars)
  2. secops eliminado (-29 skills, -29K chars)
  3. YAML budget: 63K → 10.4K chars
  4. MEMORY.md deduplicado: 86 → 40 lineas
  5. Post-compact hook actualizado

**Resultado medible**: 83% reduccion en carga de YAML, 33% menos contexto al inicio de sesion.

---

## Modelos Recomendados por Fase

| Fase | Modelo | Razon | Costo |
|------|--------|-------|-------|
| Transcripcion AI | gemini-flash | Contexto largo, gratis | $0 |
| Whisper fallback | Ollama/local | Sin subtitulos | $0 |
| Cross-analisis 1 | grok-4-1-fast | Critico, directo | ~$0.001 |
| Cross-analisis 2 | gemini-flash | Amplio conocimiento | $0 |
| Cross-analisis 3 | groq-llama | Perspectiva independiente | $0 |
| Sintesis final | Claude Opus | Orquestacion, decision | Incluido en plan |

**Costo total del protocolo: ~$0** (todo gratis excepto Claude que ya esta en plan)
