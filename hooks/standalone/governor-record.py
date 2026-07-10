#!/usr/bin/env python3
"""SessionEnd hook — alimenta la μ continua del Gobernador de Concurrencia.

Al cerrar la sesión, registra (append-only, dedup) las duraciones de los agentes
recientes al log persistente del gobernador. Así la μ crece sola entre sesiones.

FAIL-OPEN pero NO fail-silent (feedback_failopen_not_failsilent): es un WRITE-PATH;
ante error LOGUEA a governor_errors.log en vez de callar. Additivo: solo anexa a sus
propios logs; NO toca transcripts, DB, ni otros hooks.

Fuente versionada: ~/projects/aris4u/hooks/standalone/governor-record.py
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Portabilidad: CLAUDE_PLUGIN_ROOT si está seteado; sino, sube 2 niveles desde
# hooks/standalone/ para la raíz del repositorio aris4u.
_FALLBACK_REPO = Path(__file__).resolve().parents[2]
REPO = str(Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or _FALLBACK_REPO))
PY = str(Path(REPO) / ".venv312" / "bin" / "python")

# BASE: directorio de transcripts de Claude Code para este usuario.
# El subdirectorio bajo .claude/projects/ se obtiene reemplazando "/" del home por "-".
# Ej: /home/alice → -home-alice  (macOS: /Users/alice → -Users-alice)
_HOME = Path.home()
BASE = str(_HOME / ".claude" / "projects" / str(_HOME).replace("/", "-"))
ERR_LOG = os.path.expanduser("~/.claude/data/governor_errors.log")


def _log(msg: str) -> None:
    """Anexa un error al log (último recurso: si ESTO falla, ahí sí se calla)."""
    try:
        os.makedirs(os.path.dirname(ERR_LOG), exist_ok=True)
        with open(ERR_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat()} governor-record: {msg}\n")
    except OSError:
        pass


def main() -> int:
    try:
        result = subprocess.run(
            [PY, "-m", "engine.v16.orchestration.concurrency_governor",
             "--record-durations", BASE],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            _log(f"exit {result.returncode}: {result.stderr.strip()[:200]}")
    except Exception as exc:  # noqa: BLE001 — fail-open, pero LOGUEA (no silent)
        _log(f"excepción: {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
