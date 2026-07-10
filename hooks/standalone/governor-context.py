#!/usr/bin/env python3
"""UserPromptSubmit hook — inyecta la línea del Gobernador de Concurrencia en contexto.

Corre el gobernador de ARIS4U (--oneline) y su salida se añade al contexto de Claude,
para que dimensione el fan-out de agentes por tipo + RAM viva, sin que el usuario lo pida.

FAIL-OPEN: ante cualquier error/timeout no imprime nada y sale 0 — jamás bloquea el prompt.
Coexiste con los otros hooks UserPromptSubmit (no toca el dispatcher de ARIS4U).
"""
import os
import subprocess
import sys
from pathlib import Path

# Portabilidad: CLAUDE_PLUGIN_ROOT si está seteado (instalación externa); sino, sube 2
# niveles desde hooks/standalone/ para encontrar la raíz del repositorio aris4u.
_FALLBACK_REPO = Path(__file__).resolve().parents[2]
REPO = str(Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or _FALLBACK_REPO))
PY = str(Path(REPO) / ".venv312" / "bin" / "python")


def main() -> int:
    try:
        result = subprocess.run(
            [PY, "-m", "engine.v16.orchestration.concurrency_governor", "--oneline"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
        line = result.stdout.strip()
        if line:
            print(line)
    except Exception:  # noqa: BLE001 — fail-open deliberado: nunca bloquear el prompt
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
