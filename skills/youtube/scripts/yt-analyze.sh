#!/usr/bin/env bash
# yt-analyze.sh — Analizar contenido de video via transcripcion + APIs directas
# Uso: ./yt-analyze.sh "VIDEO_URL" "PREGUNTA O INSTRUCCION" [--local] [--model MODEL]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Default: Gemini API (free tier, included in subscription)
DEFAULT_MODEL="gemini-2.5-flash"

VIDEO_URL=""
PROMPT=""
MODEL="$DEFAULT_MODEL"
USE_LOCAL=false

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --local)
      USE_LOCAL=true
      shift
      ;;
    --model)
      MODEL="$2"
      shift 2
      ;;
    *)
      if [[ -z "$VIDEO_URL" ]]; then
        VIDEO_URL="$1"
      elif [[ -z "$PROMPT" ]]; then
        PROMPT="$1"
      fi
      shift
      ;;
  esac
done

if [[ -z "$VIDEO_URL" || -z "$PROMPT" ]]; then
  echo "Uso: $0 \"VIDEO_URL\" \"PREGUNTA\" [--local] [--model MODEL]" >&2
  echo "  --local    Usar Ollama local (para contenido sensible/privado)" >&2
  echo "  --model X  Usar modelo especifico" >&2
  exit 1
fi

# Obtener metadata del video
echo "--- Metadata ---" >&2
yt-dlp --print "%(title)s | %(channel)s | %(duration_string)s" --no-download "$VIDEO_URL" 2>/dev/null >&2 || true

# Extraer transcripcion
echo "--- Extrayendo transcripcion... ---" >&2
TRANSCRIPT=$("$SCRIPT_DIR/yt-transcript.sh" "$VIDEO_URL" "es" 2>/dev/null) || {
  echo "Error: No se pudo obtener transcripcion. Ver opciones de fallback arriba." >&2
  exit 2
}

TRANSCRIPT_LEN=${#TRANSCRIPT}
echo "--- Transcripcion: ${TRANSCRIPT_LEN} caracteres ---" >&2

# Truncar si es muy largo (>100K chars ~25K tokens)
if [[ $TRANSCRIPT_LEN -gt 100000 ]]; then
  echo "--- Advertencia: Transcripcion truncada a 100K caracteres ---" >&2
  TRANSCRIPT="${TRANSCRIPT:0:100000}"
fi

# Construir JSON payload en Python para evitar problemas de escaping en bash
PAYLOAD_FILE=$(mktemp /tmp/yt-payload-XXXXXX.json)
trap "rm -f '$PAYLOAD_FILE'" EXIT

if [[ "$USE_LOCAL" == true ]]; then
  # Ollama local — para contenido sensible/privado
  echo "--- Analizando con Ollama local ($MODEL)... ---" >&2

  python3 -c "
import sys, json

transcript = sys.stdin.read()
prompt = '''$PROMPT'''
model = '''$MODEL'''

payload = {
    'model': model if model != 'gemini-2.5-flash' else 'qwen2.5:3b',
    'messages': [
        {'role': 'system', 'content': 'Eres un asistente que analiza contenido de videos de YouTube. Se te proporciona la transcripcion del video. Responde en espanol.'},
        {'role': 'user', 'content': f'Transcripcion del video:\n{transcript}\n\nInstruccion: {prompt}'}
    ],
    'stream': False
}

with open('$PAYLOAD_FILE', 'w') as f:
    json.dump(payload, f)
" <<< "$TRANSCRIPT"

  RESPONSE=$(curl -s http://localhost:11434/api/chat -d @"$PAYLOAD_FILE" 2>/dev/null)
  echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if 'message' in data:
        print(data['message']['content'])
    else:
        print(json.dumps(data, indent=2))
except Exception as e:
    print(f'Error parseando respuesta: {e}', file=sys.stderr)
    sys.exit(1)
"
else
  # Gemini API directa (incluida en suscripcion)
  echo "--- Analizando con Gemini ($MODEL)... ---" >&2

  # Load API key
  GEMINI_KEY=$(grep GEMINI_API_KEY ~/CLAUDE/.env.apikeys 2>/dev/null | cut -d= -f2 || echo "")
  if [[ -z "$GEMINI_KEY" ]]; then
    # Try from W2
    GEMINI_KEY=$(ssh w2 'grep GEMINI_API_KEY ~/CLAUDE/.env.apikeys 2>/dev/null | cut -d= -f2' 2>/dev/null || echo "")
  fi

  if [[ -z "$GEMINI_KEY" ]]; then
    echo "Error: GEMINI_API_KEY no encontrada en ~/CLAUDE/.env.apikeys" >&2
    exit 3
  fi

  python3 -c "
import sys, json

transcript = sys.stdin.read()
prompt = '''$PROMPT'''

payload = {
    'contents': [{
        'parts': [{
            'text': f'Eres un asistente que analiza contenido de videos de YouTube. Se te proporciona la transcripcion del video. Responde en espanol.\n\nTranscripcion del video:\n{transcript}\n\nInstruccion: {prompt}'
        }]
    }],
    'generationConfig': {
        'temperature': 0.3
    }
}

with open('$PAYLOAD_FILE', 'w') as f:
    json.dump(payload, f)
" <<< "$TRANSCRIPT"

  RESPONSE=$(curl -s "https://generativelanguage.googleapis.com/v1beta/models/${MODEL}:generateContent?key=${GEMINI_KEY}" \
    -H "Content-Type: application/json" \
    -d "@$PAYLOAD_FILE" 2>/dev/null)

  echo "$RESPONSE" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if 'candidates' in data:
        print(data['candidates'][0]['content']['parts'][0]['text'])
    elif 'error' in data:
        print(f\"Error API: {data['error']['message']}\", file=sys.stderr)
        sys.exit(1)
    else:
        print(json.dumps(data, indent=2))
except Exception as e:
    print(f'Error parseando respuesta: {e}', file=sys.stderr)
    sys.exit(1)
"
fi
