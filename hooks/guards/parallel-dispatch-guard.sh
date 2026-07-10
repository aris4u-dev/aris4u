#!/bin/bash
#
# parallel-dispatch-guard.sh — Enforce parallel dispatch patterns
# Purpose: Detect sequential `ssh w[0-9]` calls without & and suggest parallelization (parallel-dispatch.md)
# Hook: PostToolUse on Write/Edit to bash/sh scripts
# Behavior: WARNING (non-blocking, exit 0 always)
#
# Input: JSON on stdin with tool_name, tool_input.file_path, tool_input.content
# Output: JSON on stdout with additionalContext warning (if violations found)
#

set +e  # Allow command failures for jq

# Read entire stdin as JSON
JSON_INPUT=$(cat 2>/dev/null || echo "{}")

# Extract file_path and content using jq (more robust than grep/sed)
FILE_PATH=$(echo "$JSON_INPUT" | jq -r '.tool_input.file_path // "unknown.sh"' 2>/dev/null)
NEW_CONTENT=$(echo "$JSON_INPUT" | jq -r '.tool_input.content // ""' 2>/dev/null)

# Fallback if jq not available or jq failed
if [[ -z "$FILE_PATH" || "$FILE_PATH" == "null" ]]; then
  FILE_PATH="unknown.sh"
fi

# Only apply to bash/sh scripts
if ! [[ "$FILE_PATH" =~ \.(sh|bash)$ ]]; then
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"\"}}"
  exit 0
fi

# Detect sequential ssh w[0-9] calls without &
violations=0
while IFS= read -r line; do
  # Skip empty lines, comments
  [[ -z "$(echo "$line" | tr -d '[:space:]')" ]] && continue
  [[ "$line" =~ ^[[:space:]]*# ]] && continue

  # Match ssh w[0-9] but not ending with &
  if [[ "$line" =~ ssh[[:space:]]+w[0-9] ]]; then
    if ! [[ "$line" =~ \&[[:space:]]*$ ]]; then
      ((violations++))
    fi
  fi

  # Stop checking at blocking statements
  if [[ "$line" =~ ^[[:space:]]*(wait|if|for|while) ]]; then
    if [[ $violations -gt 0 ]]; then
      break
    fi
  fi
done <<< "$NEW_CONTENT"

if [[ $violations -gt 0 ]]; then
  msg="⚠️ PARALLEL DISPATCH: $violations sequential ssh call(s) could be parallelized. Use 'ssh w2 cmd &' pattern for ~50% faster execution"
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"$msg\"}}"
else
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PostToolUse\",\"additionalContext\":\"\"}}"
fi

exit 0
