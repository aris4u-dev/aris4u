#!/bin/bash
#
# gpu-crash-guard.sh — Block opening heavy WebGL / Gaussian-splat viewers (GPU killer)
# Purpose: The WebGL gaussian-splat viewer can crash the host Mac via GPU 'progress
#          timeout' (WindowServer watchdog). Soft discipline fails — this guard HARD-BLOCKS.
# Hook: PreToolUse on Bash (command) and Playwright navigate (url)
# Behavior: BLOCKING (permissionDecision: deny) when the command would open the
#           splat viewer / port 8901 / .ply-.splat scenes in any browser on this Mac.
# Override: create ~/.aris4u/gpu-crash-override after applying mitigations;
#           the guard then warns instead of blocking.
#
# Input: JSON on stdin with tool_name, tool_input.command (Bash) or tool_input.url
# Output: JSON on stdout (deny or empty additionalContext)
#

set +e

JSON_INPUT=$(cat 2>/dev/null || echo "{}")
CMD=$(echo "$JSON_INPUT" | jq -r '.tool_input.command // ""' 2>/dev/null)
URL=$(echo "$JSON_INPUT" | jq -r '.tool_input.url // ""' 2>/dev/null)
TARGET="$CMD $URL"

allow() {
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":""}}'
  exit 0
}

# Nothing to inspect
[[ -z "${TARGET// /}" ]] && allow

# Danger signals: opening the splat viewer or splat assets in a GPU context on this Mac
VIEWER_RE='(localhost|127\.0\.0\.1):8901|scene3d/viewer|gaussian-splats|\.splat([^a-zA-Z]|$)'
PLY_IN_BROWSER_RE='\.ply([^a-zA-Z]|$)'
OPENER_RE='(^|[;&| ])open( |$)|Google Chrome|Safari|firefox|chromium|browser_navigate|playwright'

danger=0
# A URL param (browser navigation tool) pointing at the viewer is dangerous by itself
if [[ -n "$URL" ]] && echo "$URL" | grep -qiE "$VIEWER_RE|$PLY_IN_BROWSER_RE"; then
  danger=1
fi
if echo "$TARGET" | grep -qiE "$VIEWER_RE"; then
  # serving or opening the viewer / port 8901 / splat libs
  if echo "$TARGET" | grep -qiE "$OPENER_RE|http\.server|nohup|serve"; then
    danger=1
  fi
fi
# Opening .ply scenes directly in a browser
if echo "$TARGET" | grep -qiE "$PLY_IN_BROWSER_RE" && echo "$TARGET" | grep -qiE "$OPENER_RE"; then
  danger=1
fi

[[ $danger -eq 0 ]] && allow

# Human override file (created manually after mitigations are applied)
if [[ -f "$HOME/.aris4u/gpu-crash-override" ]]; then
  msg="⚠️ GPU-CRASH-GUARD (override activo): este comando toca el viewer splat. Procede SOLO si las mitigaciones están aplicadas."
  echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"additionalContext\":\"$msg\"}}"
  exit 0
fi

reason="🛑 GPU-CRASH-GUARD: BLOQUEADO. El viewer scene3d (gaussian splats WebGL, :8901) puede tumbar el Mac (GPU 'progress timeout'). NO abrir el viewer/.ply/.splat en ningún browser de este Mac. Alternativas: (1) verificación estática (node --check, grep), (2) servir y renderizar en una máquina con GPU dedicada (CUDA). Para desbloquear: crea ~/.aris4u/gpu-crash-override"
jq -n --arg r "$reason" '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":$r}}'
exit 0
