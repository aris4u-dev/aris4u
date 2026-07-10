---
name: youtube
description: >
  Busqueda en YouTube, extraccion de transcripciones y analisis/interpretacion de contenido de video
  usando herramientas locales (yt-dlp, youtube-transcript-api) y modelos AI (Gemini gratis / Ollama local).
  Use when: (1) Buscar videos en YouTube por tema o query, (2) Extraer transcripcion o subtitulos de un video,
  (3) Analizar o resumir el contenido de un video, (4) Responder preguntas sobre un video especifico.
version: 0.1.0
maintainer: aris4u
category: research
tags: [youtube, video, transcript, search, analysis]
effort: high
dependencies:
  packages: [yt-dlp, youtube-transcript-api]
  tools: [curl, jq, python3]
---

# YouTube Search & Video Interpretation

## Overview

Skill para buscar videos en YouTube, extraer transcripciones y analizar contenido de video.
Todo funciona localmente sin API keys de pago. Usa yt-dlp para busqueda/metadata,
youtube-transcript-api para subtitulos, y APIs directas (Gemini/Ollama) para interpretacion.

## Quick Start

```bash
# Buscar videos
./scripts/yt-search.sh "kubernetes tutorial 2026"

# Extraer transcripcion
./scripts/yt-transcript.sh "https://www.youtube.com/watch?v=VIDEO_ID"

# Analizar video (transcripcion + resumen AI)
./scripts/yt-analyze.sh "https://www.youtube.com/watch?v=VIDEO_ID" "Resume los puntos clave"
```

## Core Workflow

### 1. Busqueda de Videos

Usa `yt-search.sh` para encontrar videos relevantes:

```bash
./scripts/yt-search.sh "QUERY" [MAX_RESULTS]
```

- Retorna: titulo, URL, canal, duracion, vistas
- Default: 5 resultados
- Sin API key requerida (usa yt-dlp ytsearch)

### 2. Extraccion de Transcripcion

Usa `yt-transcript.sh` para obtener subtitulos/transcripcion:

```bash
./scripts/yt-transcript.sh "VIDEO_URL" [LANG]
```

- Idiomas: intenta el idioma solicitado (default: es), luego en, luego auto-generated
- Retorna texto plano con timestamps
- Sin API key requerida

### 3. Analisis con AI

Usa `yt-analyze.sh` para interpretar el contenido:

```bash
./scripts/yt-analyze.sh "VIDEO_URL" "PREGUNTA O INSTRUCCION"
```

- Extrae transcripcion automaticamente
- Envia a Gemini API directa (GEMINI_API_KEY)
- Para contenido sensible/PHI: usa flag `--local` para Ollama

**Modelo routing:**
- Default: `gemini-2.5-flash` via Gemini API directa
- Contenido sensible: Ollama local
- Modelo custom: `--model MODEL_NAME`

### Workflow Completo (video individual)

1. Buscar videos: `yt-search.sh "tema de interes"`
2. Revisar resultados, elegir video
3. Extraer transcripcion: `yt-transcript.sh "URL"`
4. Analizar contenido: `yt-analyze.sh "URL" "que quiero saber"`

### Workflow Avanzado (multi-video, herramientas, mejoras al ambiente)

Para analisis de multiples videos sobre herramientas, frameworks, o mejoras al setup:
**Cargar** `references/video-analysis-protocol.md` — protocolo completo de 6 fases con
triage, clasificacion hype vs realidad, cross-analisis multi-modelo, auditoria del ambiente,
matriz de decision, e implementacion con medicion antes/despues.

## Uso Directo por Claude (sin scripts)

Claude puede ejecutar directamente:

```bash
# Busqueda rapida
yt-dlp "ytsearch5:QUERY" --flat-playlist --print "%(title)s | %(url)s | %(channel)s | %(duration_string)s" --no-warnings

# Transcripcion rapida (Python)
python3 -c "
from youtube_transcript_api import YouTubeTranscriptApi
api = YouTubeTranscriptApi()
transcript = api.fetch('VIDEO_ID', languages=['es','en'])
for t in transcript: print(f'[{t.start:.0f}s] {t.text}')
"

# Metadata de video
yt-dlp --print "%(title)s\n%(channel)s\n%(upload_date)s\n%(duration_string)s\n%(view_count)s views\n%(description)s" --no-download "VIDEO_URL"
```

## Interpretacion de Video (sin transcripcion)

Para videos sin subtitulos disponibles:

1. Descargar audio: `yt-dlp -x --audio-format mp3 -o "/tmp/yt-audio.mp3" "URL"`
2. Transcribir con Whisper (local o remoto): `whisper /tmp/yt-audio.mp3 --model small --language es`
3. O usar Gemini para audio directo (si < 20MB)

## Limites y Consideraciones

- **Rate limits YouTube**: yt-dlp puede ser rate-limited con uso excesivo. Espaciar busquedas.
- **Transcripciones**: No todos los videos tienen subtitulos. Fallback a Whisper.
- **Gemini gratis**: 500 requests/dia Flash, 50/dia Pro. Suficiente para uso normal.
- **Privacidad**: Nunca enviar contenido sensible/PHI a APIs externas. Usar `--local`.
- **Videos largos**: Transcripciones de videos >1h pueden exceder limites de contexto. Segmentar.

## Troubleshooting

### Issue: "No transcripts found"
**Solucion**: El video no tiene subtitulos. Usar Whisper: `yt-dlp -x --audio-format mp3` + Whisper local.

### Issue: "HTTP Error 429"
**Solucion**: Rate limited por YouTube. Esperar 5-10 minutos o usar VPN.

### Issue: Transcripcion en idioma incorrecto
**Solucion**: Especificar idioma: `yt-transcript.sh "URL" en` o `--lang es`.

## Chain Triggers (Auto-sugerencias post-ejecucion)

Despues de analizar videos, Claude DEBE sugerir:
1. **multi-research** — "¿Valido estos hallazgos con otros modelos AI?"
2. **prd-generator** — Si el video es sobre una herramienta: "¿Creo un PRD para implementar esto?"
3. **xlsx-toolkit** — Si se analizaron multiples videos: "¿Genero una tabla comparativa?"

## Inputs From (Skills que alimentan este)

- **multi-research** → Temas identificados que necesitan investigacion en video
- **market-research** → Tendencias de mercado para buscar contenido en video

## Memory Integration

- Al completar: `context_save(key="youtube-{tema}-{fecha}", category="note", value="Video analizado: {titulo}, conclusion: {resumen}")`
- Guardar conclusiones de videos para referencia futura
- Evitar re-analizar videos ya procesados (buscar por URL)

## Auto-Triggers

- Si se menciona "vi un video sobre" o "hay un tutorial de" → activar
- Post multi-research: si hay divergencia entre modelos → buscar videos como fuente adicional
- Si el usuario quiere aprender sobre un tema → sugerir buscar tutoriales
