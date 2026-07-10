#!/usr/bin/env python3
"""Mantiene pyproject.toml en sync con .claude-plugin/plugin.json (fuente canónica de versión).

WS0 (deuda de trust): evita el drift que produjo pyproject=16.3.0 vs plugin=16.9.0.

Uso:
    python tools/sync_version.py           # sincroniza pyproject a la versión de plugin.json
    python tools/sync_version.py --check    # exit 1 si difieren (lo usa el pre-commit hook)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
PYPROJECT = ROOT / "pyproject.toml"


def plugin_version() -> str:
    """Lee la versión canónica desde plugin.json."""
    return json.loads(PLUGIN.read_text())["version"]


def pyproject_version(text: str) -> str | None:
    """Extrae la versión declarada en pyproject.toml, o None si no existe."""
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def main() -> int:
    """Sincroniza pyproject a la versión canónica, o verifica coherencia con --check."""
    canonical = plugin_version()
    text = PYPROJECT.read_text()
    current = pyproject_version(text)

    if current == canonical:
        print(f"OK: versiones en sync ({canonical})")
        return 0

    if "--check" in sys.argv:
        print(
            f"MISMATCH: pyproject={current} vs plugin={canonical}. "
            "Corre: python tools/sync_version.py",
            file=sys.stderr,
        )
        return 1

    new_text = re.sub(
        r'^(version\s*=\s*")[^"]+(")',
        rf"\g<1>{canonical}\g<2>",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    PYPROJECT.write_text(new_text)
    print(f"Sincronizado pyproject {current} -> {canonical}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
