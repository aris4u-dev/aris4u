"""Handler commit_quality_gate — Gate de calidad en el COMMIT (PostToolUse Bash + git commit).

Fase 2 del gate de calidad (complementa code_quality_gate, que actúa en cada edit).
En el commit corre los checks más caros que serían ruidosos por-edit:
  - pyright sobre los .py cambiados en el commit → errores de tipo.
  - tests AFECTADOS: para cada .py del commit busca su test (test_<nombre>.py) y lo corre.

Post-commit (el commit ya pasó): emite advisory para arreglar en el siguiente cambio,
NO bloquea (consistente con el contrato advisory del dispatcher). Registra en gate_results.
Fail-open total. Universal: detecta el pytest del repo; si no hay, corre solo pyright.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone, UTC

from dispatch.contract import ARIS4U_ROOT

_SKIP_MARKERS = ("/.venv", "/site-packages/", "/node_modules/", "/__pycache__/")


def _changed_py_files(repo: str) -> list[str]:
    """Archivos .py tocados en el commit HEAD (rutas absolutas existentes)."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "show", "--name-only", "--pretty=format:", "HEAD"],
            capture_output=True,
            text=True,
            timeout=8,
        ).stdout
    except Exception:
        return []
    files = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel.endswith(".py") or any(m in rel for m in _SKIP_MARKERS):
            continue
        ap = os.path.join(repo, rel)
        if os.path.isfile(ap):
            files.append(ap)
    return files


def _run_pyright(files: list[str]) -> int:
    """Errores de tipo (pyright) sobre los archivos. -1 si pyright no disponible/error."""
    exe = shutil.which("pyright")
    if not exe or not files:
        return -1
    try:
        proc = subprocess.run(
            [exe, "--outputjson", *files],
            capture_output=True,
            text=True,
            timeout=45,
        )
        data = json.loads(proc.stdout or "{}")
        return int(data.get("summary", {}).get("errorCount", 0))
    except Exception:
        return -1


def _find_pytest(repo: str) -> str:
    """Localiza un pytest del repo (venv). "" si no hay."""
    for pat in (".venv*/bin/pytest", "venv/bin/pytest", "env/bin/pytest"):
        hits = glob.glob(os.path.join(repo, pat))
        if hits:
            return hits[0]
    return ""


def _affected_tests(repo: str, files: list[str]) -> list[str]:
    """Test files (test_<nombre>.py) relacionados con los .py cambiados."""
    tests: set[str] = set()
    for f in files:
        base = os.path.basename(f)[:-3]  # sin .py
        if base.startswith("test_"):
            tests.add(f)
            continue
        for hit in glob.glob(os.path.join(repo, "**", f"test_{base}.py"), recursive=True):
            if "/.venv" not in hit:
                tests.add(hit)
    return sorted(tests)


def _run_tests(pytest_bin: str, tests: list[str]) -> tuple[int, int]:
    """Corre los tests afectados. Devuelve (passed, failed); (-1,-1) si no se pudo."""
    if not pytest_bin or not tests:
        return (-1, -1)
    try:
        proc = subprocess.run(
            [pytest_bin, *tests, "-q", "--no-header", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return (-1, -1)
    out = (proc.stdout or "") + (proc.stderr or "")
    mp = re.search(r"(\d+) passed", out)
    mf = re.search(r"(\d+) failed", out)
    passed = int(mp.group(1)) if mp else 0
    failed = int(mf.group(1)) if mf else 0
    return (passed, failed)


def _record(repo: str, status: str, details: dict) -> None:
    """Registra el resultado en gate_results. Best-effort, fail-open."""
    db = ARIS4U_ROOT / "data" / "sessions.db"
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(str(db), timeout=2.0)
        try:
            conn.execute(
                "INSERT INTO gate_results (module_name, timestamp, status, details, session_ref) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    f"commit:{os.path.basename(repo)}"[:200],
                    datetime.now(UTC).isoformat(),
                    status,
                    json.dumps(details)[:2000],
                    os.environ.get("ARIS4U_SESSION_ID", ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def run(tool_name: str, tool_input: dict, cwd: str) -> str:
    """Gate de calidad en commit. Devuelve advisory ("" si N/A o todo limpio)."""
    if tool_name != "Bash":
        return ""
    if "git commit" not in ((tool_input or {}).get("command") or ""):
        return ""
    repo = cwd or os.getcwd()
    files = _changed_py_files(repo)
    if not files:
        return ""

    type_errors = _run_pyright(files)
    pytest_bin = _find_pytest(repo)
    tests = _affected_tests(repo, files)
    passed, failed = _run_tests(pytest_bin, tests)

    parts: list[str] = []
    if type_errors > 0:
        parts.append(f"  • pyright: {type_errors} error(es) de tipo en archivos del commit")
    if failed > 0:
        parts.append(
            f"  • tests afectados: {failed} FALLANDO ({passed} pass) — revisar antes del próximo cambio"
        )
    elif passed > 0:
        parts.append(f"  • tests afectados: {passed} pass ✓")

    _record(
        repo,
        "issues" if (type_errors > 0 or failed > 0) else "clean",
        {
            "type_errors": max(type_errors, 0),
            "tests_passed": max(passed, 0),
            "tests_failed": max(failed, 0),
            "n_files": len(files),
        },
    )

    if type_errors > 0 or failed > 0:
        return f"🔎 Commit quality gate ({len(files)} archivo·s .py):\n" + "\n".join(parts)
    return ""
