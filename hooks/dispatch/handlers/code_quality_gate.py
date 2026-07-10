"""Handler code_quality_gate — Gate de calidad de código en PostToolUse Write|Edit|MultiEdit.

Contramedida directa a la DEGRADACIÓN ITERATIVA (SlopCodeBench: el código de agentes
erosiona en loops — complejidad ×2.2-2.5 por 8 ciclos). Tras cada escritura de un .py,
corre linters rápidos (ruff) + complejidad ciclomática (radon) SOBRE EL ARCHIVO TOCADO
y emite un additionalContext advisory con hallazgos accionables. Registra el resultado
en la tabla `gate_results` (telemetría, antes inerte).

Diseño:
  - Solo Write/Edit/MultiEdit a archivos .py reales (excluye venvs/site-packages/cache).
  - NO corre tests aquí (sería lento en cada edit); el gate de tests engancha en commit.
  - Usa ruff/radon del venv de aris4u (.venv312) → universal, no depende del venv target.
  - Advisory puro (additionalContext). Fail-open total: cualquier error → "" (no bloquea).

Patrón de handler idéntico a schema_drift/capture_commit: `run()` devuelve un string para
que el orquestador post_tool_use lo agregue al additionalContext combinado.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone, UTC

from dispatch.contract import ARIS4U_ROOT


# Binarios de calidad instalados en el venv de aris4u (universales para cualquier .py).
# Producción usa .venv312; si no está (CI/instalación distinta), cae al PATH.
def _resolve_bin(name: str) -> str:
    """Prefiere el binario del .venv312 de aris4u; si no existe, busca en el PATH."""
    venv_bin = ARIS4U_ROOT / ".venv312" / "bin" / name
    if venv_bin.is_file():
        return str(venv_bin)
    return shutil.which(name) or str(venv_bin)


_RUFF = _resolve_bin("ruff")
_RADON = _resolve_bin("radon")

# Umbral de complejidad ciclomática a partir del cual una función es "hotspot" (grado C+).
# radon: A≤5, B≤10, C≤20, D≤30, E≤40, F>40.
# Recalibrado 11→15 (2026-06-24): el umbral 11 flaggeaba TODA función grado-C (moderada),
# haciendo que 38 módulos del motor "nunca pasen" el gate solo por complejidad 11-14 aceptable
# (lint=0 en todos). 15 flaggea la C-alta y peor (lo que sí vale refactorizar), dejando pasar
# la complejidad moderada. Evidencia: distribución worst_cc + medidor Calidad de la consola.
_COMPLEXITY_THRESHOLD = 15

# Rutas que nunca se chequean (dependencias / generado).
_SKIP_MARKERS = ("/.venv", "/site-packages/", "/node_modules/", "/__pycache__/", "/.git/")


def _is_target(file_path: str) -> bool:
    """True si el archivo es un .py real del usuario (no dependencia/generado)."""
    if not file_path.endswith(".py"):
        return False
    if any(marker in file_path for marker in _SKIP_MARKERS):
        return False
    return os.path.isfile(file_path)


def _run_ruff(file_path: str) -> list[str]:
    """Corre ruff sobre el archivo. Devuelve líneas de issue concisas ([] si limpio/error)."""
    if not os.path.isfile(_RUFF):
        return []
    try:
        proc = subprocess.run(
            [_RUFF, "check", file_path, "--output-format", "concise", "--quiet"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    return [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]


def _run_radon(file_path: str) -> list[tuple[str, int]]:
    """Corre radon cc sobre el archivo. Devuelve [(nombre_funcion, complejidad)] grado C+."""
    if not os.path.isfile(_RADON):
        return []
    try:
        proc = subprocess.run(
            [_RADON, "cc", file_path, "-s", "-j"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        data = json.loads(proc.stdout or "{}")
    except Exception:
        return []
    hotspots: list[tuple[str, int]] = []
    for blocks in data.values():
        if not isinstance(blocks, list):
            continue
        for blk in blocks:
            cc = int(blk.get("complexity", 0) or 0)
            if cc >= _COMPLEXITY_THRESHOLD:
                hotspots.append((blk.get("name", "?"), cc))
    hotspots.sort(key=lambda x: x[1], reverse=True)
    return hotspots


def _record(file_path: str, status: str, details: dict) -> None:
    """Registra el resultado del gate en gate_results (telemetría). Best-effort, fail-open."""
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
                    os.path.basename(file_path)[:200],
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


def _build_ruff_lines(issues: list[str]) -> list[str]:
    """Construye las líneas de detalle ruff para el advisory (máx 5 mostradas)."""
    shown = issues[:5]
    lines: list[str] = [f"  • ruff: {len(issues)} issue(s)" + " (ruff --fix arregla la mayoría)"]
    for ln in shown:
        # ln viene como path:line:col: CODE msg → recorto el path para legibilidad
        lines.append(
            "      " + ln.split(":", 1)[-1].strip()[:120] if ":" in ln else "      " + ln[:120]
        )
    if len(issues) > len(shown):
        lines.append(f"      … +{len(issues) - len(shown)} más")
    return lines


def _build_advisory(name: str, issues: list[str], hotspots: list[tuple[str, int]]) -> str:
    """Ensambla el texto advisory completo a partir de los hallazgos de ruff y radon."""
    parts: list[str] = [f"🔎 Code quality gate — {name}:"]
    if issues:
        parts.extend(_build_ruff_lines(issues))
    if hotspots:
        hs = ", ".join(f"{nm} (CC={cc})" for nm, cc in hotspots[:4])
        parts.append(f"  • complejidad alta: {hs} — considera refactor (degradación iterativa)")
    return "\n".join(parts)


def run(tool_name: str, tool_input: dict | None) -> str:
    """Corre el gate de calidad sobre el .py tocado. Devuelve additionalContext ("" si N/A).

    Args:
        tool_name: nombre del tool (solo Write/Edit/MultiEdit actúan).
        tool_input: tool_input del evento (usa file_path).

    Returns:
        Texto advisory para additionalContext, o "" si no aplica / código limpio.
    """
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return ""
    file_path = (tool_input or {}).get("file_path") or ""
    if not _is_target(file_path):
        return ""

    issues = _run_ruff(file_path)
    hotspots = _run_radon(file_path)

    if not issues and not hotspots:
        _record(file_path, "clean", {"lint": 0, "hotspots": 0})
        return ""

    _record(
        file_path,
        "issues",
        {
            "lint": len(issues),
            "hotspots": len(hotspots),
            "worst_cc": hotspots[0][1] if hotspots else 0,
        },
    )
    return _build_advisory(os.path.basename(file_path), issues, hotspots)
