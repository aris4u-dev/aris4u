---
name: transcribe
description: >
  Transcribe audio/video files to text using mlx-whisper (Apple Silicon, runs 100% local,
  no cloud, PHI-safe). Accepts any audio/video format ffmpeg can handle.
  Outputs: plain text, SRT subtitles, or timestamped JSON.
  Use when: (1) meeting recordings, (2) voice notes, (3) video subtitles,
  (4) PHI-safe medical dictation (healthcare clients), (5) Instagram Reels audio.
  Motor: mlx-whisper 0.4.3 en venv312 de aris4u (M5 MPS, ~10x CPU).
version: 1.0.0
category: audio
tags: [transcribe, audio, whisper, mlx, local, phi-safe]
---

# Transcribe — Audio/Video to Text (local, PHI-safe)

## Setup (verificado 2026-07-03)
- mlx-whisper 0.4.3 instalado en `~/projects/aris4u/.venv312/`
- Modelos descargables automáticamente la primera vez (caché en `~/.cache/huggingface/hub/`)
- ffmpeg requerido para formatos no-WAV: `brew install ffmpeg` (ya instalado si tienes yt-dlp)

## Uso básico

```bash
# Transcripción simple → texto
~/projects/aris4u/.venv312/bin/python3 -m mlx_whisper \
  --model mlx-community/whisper-large-v3-turbo \
  --output-format txt \
  archivo.mp4

# Con timestamps → SRT (para subtítulos de video)
~/projects/aris4u/.venv312/bin/python3 -m mlx_whisper \
  --model mlx-community/whisper-large-v3-turbo \
  --output-format srt \
  archivo.mp4

# JSON completo con timestamps por segmento
~/projects/aris4u/.venv312/bin/python3 -m mlx_whisper \
  --model mlx-community/whisper-large-v3-turbo \
  --output-format json \
  --word-timestamps \
  archivo.mp4
```

## Modelos recomendados

| Modelo | Tamaño | Cuándo |
|--------|--------|--------|
| `mlx-community/whisper-large-v3-turbo` | ~1.5GB | **Default** — equilibrio velocidad/calidad |
| `mlx-community/whisper-small` | ~250MB | Notas rápidas, baja precisión |
| `mlx-community/whisper-large-v3` | ~3GB | Máxima calidad, médico/legal |

El modelo se descarga la primera vez y queda en caché local.

## Flujo para Instagram Reels (PHI-free)

```bash
# 1. Descargar audio de reel (requiere cookies/login)
yt-dlp --extract-audio --audio-format mp3 -o "reel.mp3" "<URL>"

# 2. Transcribir
~/projects/aris4u/.venv312/bin/python3 -m mlx_whisper \
  --model mlx-community/whisper-large-v3-turbo \
  --language es \
  --output-format txt \
  reel.mp3
```

## Procedimiento para este skill

1. El usuario proporciona la ruta del archivo de audio/video (o URL si yt-dlp está disponible)
2. Verificar que el archivo existe y es accesible
3. Elegir modelo según la tarea (default: whisper-large-v3-turbo)
4. Ejecutar mlx_whisper con el formato de salida apropiado
5. Mostrar la transcripción o la ruta del archivo de salida
6. Si hay más de un archivo, procesar en secuencia (no paralelizar — GPU comparte memoria)

## PHI / Healthcare

mlx-whisper runs 100% locally on the M5. Safe for medical dictations (healthcare clients).
El audio NUNCA sale de la máquina. Cumple HIPAA para procesamiento local.
For healthcare sessions with PHI: confirm `ARIS4U_HEALTHCARE=1` before processing.
