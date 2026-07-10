#!/usr/bin/env python3
"""V16.2 H5 Phase 2 + V16.3 H26 — Cosmetic diff classifier (multi-stack).

Consumes a Claude Code PreToolUse hook payload on stdin and writes the
cosmetic ratio (0-100) on stdout. Exits 0 regardless so callers can fail
soft. Used by hooks/primacy_guard.sh to decide whether a Write/Edit
is cosmetic drift vs. functional work.

H26 (2026-04-24): delegates to `tools/stack_registry` for per-stack
pattern sets. Detects stack from the `file_path` in the payload (marker
files + extension fallback). Previously used hardcoded Dart/Flutter
patterns for every file; now Java, TypeScript, Python, Rust, Go files
get their own set via `get_cosmetic_patterns(file_path)`.

Extracted from inline python3 -c heredoc inside primacy_guard.sh
(session 0423d) after a raw-string quote-escape syntax error caused the
inline script to silently return 0 on every invocation.

Design: the ratio is cosmetic_hits / (cosmetic_hits + functional_hits) *
100, rounded to int. A threshold (70 recommended) gates the warn. If
neither set matches (e.g. pure whitespace diff), emits 0.

Usage:
    echo '<hook-payload-json>' | python3 tools/cosmetic_classifier.py
"""

from __future__ import annotations

import json
import os
import re
import sys

# Allow importing `stack_registry` when invoked directly as a script
# (no package context). Prepend the parent directory of this file.
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from stack_registry import get_cosmetic_patterns  # noqa: E402


def classify(old_string: str, new_string: str, file_path: str = "") -> int:
    """Return cosmetic ratio 0-100 for an Edit diff.

    H26: when `file_path` is provided, regex patterns are picked from
    the stack detected at that path (flutter/node_ts/java_spring/python/
    rust/go/generic). Without a file_path, falls back to `generic`
    (union of all stacks).

    Args:
        old_string: Edit diff's `old_string` (pre-change content).
        new_string: Edit diff's `new_string` (post-change content).
        file_path: Absolute/relative path of the file being edited.
                   Used for stack detection. Empty → generic.

    Returns:
        Integer 0-100. 0 means "no signal" (pure whitespace, too short,
        or no patterns match).
    """
    combined = f"{old_string}\n{new_string}"
    if len(combined) < 20:
        return 0
    cosmetic_patterns, functional_patterns = get_cosmetic_patterns(file_path or "")
    cosmetic_hits = sum(len(re.findall(p, combined)) for p in cosmetic_patterns)
    functional_hits = sum(len(re.findall(p, combined)) for p in functional_patterns)
    total = cosmetic_hits + functional_hits
    if total == 0:
        return 0
    return int((cosmetic_hits / total) * 100)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        print(0)
        return 0
    tool_input = payload.get('tool_input') or {}
    old = tool_input.get('old_string', '') or ''
    new = tool_input.get('new_string', '') or ''
    path = tool_input.get('file_path', '') or tool_input.get('notebook_path', '') or ''
    print(classify(old, new, path))
    return 0


if __name__ == '__main__':
    sys.exit(main())
