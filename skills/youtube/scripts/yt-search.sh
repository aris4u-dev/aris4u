#!/usr/bin/env bash
# yt-search.sh — Buscar videos en YouTube via yt-dlp
# Uso: ./yt-search.sh "QUERY" [MAX_RESULTS]

set -euo pipefail

QUERY="${1:?Uso: $0 \"QUERY\" [MAX_RESULTS]}"
MAX="${2:-5}"

yt-dlp "ytsearch${MAX}:${QUERY}" \
  --flat-playlist \
  --print "%(title)s | %(url)s | %(channel)s | %(duration_string)s | %(view_count)s views" \
  --no-warnings 2>/dev/null
