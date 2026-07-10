#!/usr/bin/env python3
"""Gate de análisis estático NATIVO de Claude Code (PostToolUse, advisory, solo-lectura).

Tras cada Write/Edit/MultiEdit a un archivo de código, corre el analizador nativo del
lenguaje sobre el proyecto/archivo y REINYECTA los diagnósticos (lo mismo que VS Code
muestra en vivo) al modelo en el MISMO turno, vía additionalContext.

Por qué existe: el loop de edición nunca ejecutaba el analizador del lenguaje, así que
los diagnósticos nivel-IDE eran invisibles (measured: lab-project-1 63 issues, client-c 97 eslint,
todos invisibles al modelo durante la sesión). Este hook cierra esa brecha SIN tocar
código (read-only: reporta, no arregla; el autofix vive en el gate de cierre / Fase 2).

Frontera de responsabilidades (sin duplicar):
  - Python (.py)  -> lo cubre ARIS4U code_quality_gate (ruff per-edit) + commit gate (pyright).
                     Este hook NO toca .py para no duplicar ruff.
  - Dart/TS/Astro/Java -> ESTE hook (tenían CERO cobertura per-edit).
  - Navegación semántica -> tool LSP nativo de Claude Code (no es trabajo de este hook).

Contrato: advisory puro (additionalContext + exit 0). Fail-open TOTAL: cualquier error,
toolchain ausente o timeout -> sin output (exit 0), log a stderr. NUNCA bloquea el tool.
Es el bus NATIVO de Claude Code (settings.json), desacoplado de la salud de ARIS4U.

Portabilidad: sin paths hardcodeados. Fuente versionada: hooks/standalone/static-analysis-gate.py.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT = 12          # s por analizador; warm flutter analyze ~1.8s, margen para cold-start
MAX_SHOWN = 5         # máx diagnósticos mostrados (errores+warnings priorizados)
SKIP_SEGMENTS = {
    "node_modules", ".dart_tool", "build", ".venv", "venv", "dist", ".git",
    "__pycache__", ".next", ".turbo", ".svelte-kit", "vendor", "site-packages",
}
GEN_SUFFIXES = (".g.dart", ".freezed.dart", ".gen.dart", ".config.dart", ".mocks.dart")


def log(msg: str) -> None:
    print(f"[static-analysis-gate] {msg}", file=sys.stderr)


def find_root(start: Path, markers: list[str]) -> Path | None:
    """Sube desde start buscando el primer ancestro que contenga alguno de los markers."""
    for p in [start, *start.parents]:
        for m in markers:
            if (p / m).exists():
                return p
    return None


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=TIMEOUT
        )
    except (subprocess.TimeoutExpired, OSError) as e:  # fail-open por analizador
        log(f"analyzer error/timeout: {cmd[0]} -> {e}")
        return None


# --- Analizadores: devuelven dict(label,diags,n_err,n_warn,n_total) o None ---

def analyze_dart(f: Path) -> dict | None:
    root = find_root(f.parent, ["pubspec.yaml"])
    if root is None or not shutil.which("flutter"):
        return None
    proc = _run(["flutter", "analyze", "--no-pub"], root)
    if proc is None:
        return None
    diags = []
    n_err = n_warn = n_info = 0
    for ln in proc.stdout.splitlines():
        s = ln.strip()
        if " • " not in s:
            continue
        sev = s.split(" • ", 1)[0].strip().lower()
        if sev not in ("error", "warning", "info"):
            continue
        if sev == "error":
            n_err += 1
        elif sev == "warning":
            n_warn += 1
        else:
            n_info += 1
        diags.append((sev, s))
    total = n_err + n_warn + n_info
    if total == 0:
        return None
    return {
        "label": f"flutter analyze ({root.name})",
        "diags": diags,
        "n_err": n_err,
        "n_warn": n_warn,
        "n_total": total,
    }


def _resolve_local_bin(root: Path, name: str) -> str | None:
    cand = root / "node_modules" / ".bin" / name
    if cand.exists():
        return str(cand)
    return shutil.which(name)


def analyze_ts(f: Path) -> dict | None:
    root = find_root(f.parent, ["package.json", "tsconfig.json"])
    if root is None:
        return None
    eslint = _resolve_local_bin(root, "eslint")
    if eslint is None:
        log(f"eslint no instalado en {root} (fail-open)")
        return None
    proc = _run([eslint, "-f", "json", str(f)], root)
    if proc is None or not proc.stdout.strip():
        return None
    try:
        results = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    diags = []
    n_err = n_warn = 0
    for res in results:
        for m in res.get("messages", []):
            sev = "error" if m.get("severity") == 2 else "warning"
            if sev == "error":
                n_err += 1
            else:
                n_warn += 1
            loc = f"{f.name}:{m.get('line', '?')}:{m.get('column', '?')}"
            rule = m.get("ruleId") or ""
            diags.append((sev, f"{sev} • {m.get('message', '').strip()} • {loc} • {rule}"))
    total = n_err + n_warn
    if total == 0:
        return None
    return {
        "label": f"eslint ({root.name})",
        "diags": diags,
        "n_err": n_err,
        "n_warn": n_warn,
        "n_total": total,
    }


def analyze_astro(f: Path) -> dict | None:
    root = find_root(f.parent, ["astro.config.mjs", "astro.config.ts", "package.json"])
    if root is None:
        return None
    astro = _resolve_local_bin(root, "astro")
    if astro is None:
        log(f"astro CLI no instalado en {root} (fail-open)")
        return None
    proc = _run([astro, "check"], root)
    if proc is None:
        return None
    out = (proc.stdout or "") + (proc.stderr or "")
    diags = []
    n_err = n_warn = 0
    for ln in out.splitlines():
        s = ln.strip()
        low = s.lower()
        if "error" in low and (":" in s):
            n_err += 1
            diags.append(("error", f"error • {s}"))
        elif "warning" in low and (":" in s):
            n_warn += 1
            diags.append(("warning", f"warning • {s}"))
    total = n_err + n_warn
    if total == 0:
        return None
    return {
        "label": f"astro check ({root.name})",
        "diags": diags,
        "n_err": n_err,
        "n_warn": n_warn,
        "n_total": total,
    }


def analyze_jvm(f: Path) -> dict | None:
    """Java/Kotlin: compila vía gradle/maven wrapper si existe. Best-effort, fail-open."""
    root = find_root(f.parent, ["gradlew", "pom.xml", "build.gradle", "build.gradle.kts"])
    if root is None:
        return None
    if (root / "gradlew").exists():
        cmd = ["./gradlew", "-q", "compileJava"]
    elif (root / "pom.xml").exists() and shutil.which("mvn"):
        cmd = ["mvn", "-q", "compile"]
    else:
        return None
    proc = _run(cmd, root)
    if proc is None:
        return None
    out = (proc.stderr or "") + (proc.stdout or "")
    diags = [("error", f"error • {ln.strip()}") for ln in out.splitlines() if "error:" in ln.lower()]
    if not diags:
        return None
    return {
        "label": f"{cmd[0]} compile ({root.name})",
        "diags": diags,
        "n_err": len(diags),
        "n_warn": 0,
        "n_total": len(diags),
    }


DISPATCH = {
    ".dart": analyze_dart,
    ".ts": analyze_ts,
    ".tsx": analyze_ts,
    ".js": analyze_ts,
    ".jsx": analyze_ts,
    ".mjs": analyze_ts,
    ".cjs": analyze_ts,
    ".astro": analyze_astro,
    ".java": analyze_jvm,
    ".kt": analyze_jvm,
}


def is_skippable(f: Path) -> bool:
    if any(seg in SKIP_SEGMENTS for seg in f.parts):
        return True
    if f.name.endswith(GEN_SUFFIXES):
        return True
    return False


def emit(context: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": context,
        }
    }))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # sin payload válido -> no-op

    tool = payload.get("tool_name", "")
    if tool not in ("Write", "Edit", "MultiEdit"):
        sys.exit(0)

    fp = (payload.get("tool_input") or {}).get("file_path", "")
    if not fp:
        sys.exit(0)
    f = Path(fp)
    ext = f.suffix.lower()
    if ext not in DISPATCH or is_skippable(f) or not f.is_file():
        sys.exit(0)

    try:
        result = DISPATCH[ext](f)
    except Exception as e:  # fail-open ante cualquier error del analizador
        log(f"dispatch error: {e}")
        sys.exit(0)

    if not result or result["n_total"] == 0:
        sys.exit(0)

    # Priorizar errores > warnings > info para el top-N mostrado.
    order = {"error": 0, "warning": 1, "info": 2}
    shown = sorted(result["diags"], key=lambda d: order.get(d[0], 3))[:MAX_SHOWN]
    lines = [f"  {d[1]}" for d in shown]
    n_err, n_warn, n_total = result["n_err"], result["n_warn"], result["n_total"]
    extra = n_total - len(shown)
    header = (
        f"🔎 ANÁLISIS ESTÁTICO ({result['label']}) — diagnósticos nivel-IDE del archivo "
        f"recién editado (lo que VS Code muestra). Advisory: NO se modificó código."
    )
    footer = (
        f"→ {n_total} issue(s): {n_err} error(es), {n_warn} warning(s)."
        + (f" (+{extra} más no mostrados)" if extra > 0 else "")
        + " Resuélvelos antes de declarar el trabajo 'done'."
    )
    emit("\n".join([header, *lines, footer]))
    sys.exit(0)


if __name__ == "__main__":
    main()
