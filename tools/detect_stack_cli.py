#!/usr/bin/env python3
"""CLI wrapper around stack_registry.detect_stack — H31 (V16.5).

Lets bash hooks delegate stack detection to the canonical registry instead
of hardcoding path patterns. Hooks invoke as:

    STACK=$(python3 tools/detect_stack_cli.py "$FILE_PATH_OR_DIR")

Output is one of: flutter, java_spring, node_ts, python, generic.
Always prints exactly one token to stdout (newline-terminated). Exit 0
on success, exit 1 on missing arg.

This script intentionally has no extra deps so it can run from a venv-less
shell context (it adds tools/ to sys.path itself).
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("generic")
        return 1

    target = sys.argv[1]

    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    try:
        from stack_registry import detect_stack
    except ImportError:
        print("generic")
        return 0

    try:
        stack = detect_stack(target)
    except Exception:
        stack = "generic"

    print(stack or "generic")
    return 0


if __name__ == "__main__":
    sys.exit(main())
