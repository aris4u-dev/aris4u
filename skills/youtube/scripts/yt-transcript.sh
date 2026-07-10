#!/usr/bin/env bash
# yt-transcript.sh — Extraer transcripcion/subtitulos de un video de YouTube
# Uso: ./yt-transcript.sh "VIDEO_URL" [LANG]

set -euo pipefail

VIDEO_URL="${1:?Uso: $0 \"VIDEO_URL\" [LANG]}"
LANG="${2:-es}"

# Extraer VIDEO_ID de la URL
VIDEO_ID=$(echo "$VIDEO_URL" | grep -oP '(?:v=|youtu\.be/|/shorts/)([a-zA-Z0-9_-]{11})' | head -1 | sed 's/^v=//;s/^.*\///')

if [[ -z "$VIDEO_ID" ]]; then
  echo "Error: No se pudo extraer VIDEO_ID de: $VIDEO_URL" >&2
  exit 1
fi

VENV_PYTHON="${YOUTUBE_VENV_PYTHON:-python3}"

python3 -c "
import sys
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import NoTranscriptFound, TranscriptsDisabled

api = YouTubeTranscriptApi()
video_id = '${VIDEO_ID}'
langs = ['${LANG}', 'en', 'es']

try:
    transcript = api.fetch(video_id, languages=langs)
    for t in transcript:
        print(f'[{t.start:.0f}s] {t.text}')
except TranscriptsDisabled:
    print('Error: Subtitulos deshabilitados para este video.', file=sys.stderr)
    print('Fallback: yt-dlp -x --audio-format mp3 -o /tmp/yt-audio.mp3 \"${VIDEO_URL}\"', file=sys.stderr)
    print('Luego: whisper /tmp/yt-audio.mp3 --model small --language ${LANG}', file=sys.stderr)
    sys.exit(2)
except NoTranscriptFound:
    print('Error: No se encontraron subtitulos en ningun idioma.', file=sys.stderr)
    print('Fallback: yt-dlp -x --audio-format mp3 -o /tmp/yt-audio.mp3 \"${VIDEO_URL}\"', file=sys.stderr)
    print('Luego: whisper /tmp/yt-audio.mp3 --model small --language ${LANG}', file=sys.stderr)
    sys.exit(2)
except Exception as e:
    print(f'Error: {e}', file=sys.stderr)
    sys.exit(1)
"
