#!/usr/bin/env python3
"""
V16.3 H19 + H20 — Agent Output Verifier

Runs real verification on files an agent shipped. Catches the Fase B pattern
where agents self-report "analyze passes / tests pass / compile OK" while
reality differs (pub resolution failed, tests never ran, tests are broken).

Called from post_agent_verify.sh (Stop hook) with a list of changed files
and the repo root. Emits a single JSON line summarizing verification results.

Usage:
    python3 agent_output_verifier.py <repo_root> <file1> [<file2> ...]

Exit codes:
    0  — all checks passed (or nothing to verify)
    1  — real errors detected (agent claims diverge from reality)
    2  — verifier error (e.g., repo path invalid)

Output format (stdout):
    Single JSON line like {"verified": N, "errors": [...], "warnings": [...],
                           "pub_ok": bool, "broken_tests": [...]}
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from collections.abc import Callable

from _logger import emit_event

# V16.10 H44: soft_reward_loop integration for Q-loop
# Record reward signals from verification outcomes for adaptation
_SOFT_REWARD_LOOP_AVAILABLE = False
_SOFT_REWARD_AVAILABLE = False
try:
    # Add engine/v16 to path for soft_reward imports
    _ENGINE_V16 = Path(__file__).parent.parent / "engine" / "v16"
    if _ENGINE_V16.is_dir() and str(_ENGINE_V16) not in sys.path:
        sys.path.insert(0, str(_ENGINE_V16))
    from soft_reward import (  # noqa: F401  (probe de disponibilidad de soft_reward)
        update_verify_score,
    )
    from soft_reward_loop import record_reward

    _SOFT_REWARD_AVAILABLE = True
    _SOFT_REWARD_LOOP_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    pass  # soft_reward modules not available; continue without it

# H26 multi-stack dispatch: detect_stack identifies repo type via marker files.
# Allows verifier to dispatch dependency-resolution + per-file compile to the
# right toolchain (flutter / mvn / npm-tsc / py_compile) instead of being
# Flutter-exclusive (F37 in C3 findings).
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
try:
    from stack_registry import detect_stack
except ImportError:

    def detect_stack(
        file_path: str,
    ) -> str:  # fallback if registry missing; param name matches real import
        return "generic"


# Files relevant per stack — drives whether dependency check fires.
_STACK_EXTENSIONS: Dict[str, set[str]] = {
    "flutter": {".dart"},
    "node_ts": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte", ".astro"},
    "prisma_ts": {".ts", ".tsx", ".js", ".jsx"},
    "java_spring": {".java", ".kt", ".kts"},
    "python": {".py"},
    "django": {".py"},
    "rust": {".rs"},
    "go": {".go"},
    "generic": set(),
}


def _files_match_stack(files: List[Path], stack: str) -> bool:
    """Return True if any file extension is relevant to the detected stack."""
    exts = _STACK_EXTENSIONS.get(stack, set())
    if not exts:
        return False
    return any(f.suffix.lower() in exts for f in files)


# ---------------------------------------------------------------------------
# Check 1 — dependency resolution health (multi-stack dispatch)
# ---------------------------------------------------------------------------
def check_dependency_resolution(repo_root: Path, stack: str) -> Dict[str, object]:
    """Verify the repo's dependency manifest resolves cleanly.

    Dispatches by stack:
      - flutter      → `flutter pub get --offline`
      - node_ts      → `npm ls --depth=0 --silent` (no install, fast)
      - prisma_ts    → same as node_ts
      - java_spring  → `mvn -q -o validate`
      - others       → skipped (no-op success)

    Why per-stack: F37 found V16.2 verifier was Flutter-exclusive — Java/TS
    repos always reported "ok" because pubspec.yaml was missing (skip
    triggered). Multi-stack closes that hole for Java/TS repo work.
    """
    if stack == "flutter":
        return _check_flutter_pub(repo_root)
    if stack in ("node_ts", "prisma_ts"):
        return _check_node_npm(repo_root)
    if stack == "java_spring":
        return _check_maven(repo_root)
    if stack == "python":
        return {"ok": True, "reason": "python venvs are heterogeneous (skipped)"}
    return {"ok": True, "reason": f"no dependency check for stack '{stack}'"}


def check_pub_resolution(repo_root: Path) -> Dict[str, object]:
    """Backward-compat alias for callers that pre-date H26 multi-stack."""
    return _check_flutter_pub(repo_root)


def _check_flutter_pub(repo_root: Path) -> Dict[str, object]:
    pubspec = repo_root / "pubspec.yaml"
    if not pubspec.exists():
        return {"ok": True, "reason": "no pubspec.yaml (not a Flutter repo)"}
    flutter_bin = shutil.which("flutter")
    if flutter_bin is None:
        return {"ok": True, "reason": "flutter binary not in PATH (skipped)"}
    try:
        proc = subprocess.run(
            [flutter_bin, "pub", "get", "--offline"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "pub get timeout (>60s)"}

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lower = combined.lower()
    failure_markers = [
        "version solving failed",
        "failed to update packages",
        "because package",
        "error: could not find package",
    ]
    for marker in failure_markers:
        if marker in lower:
            lines = [ln for ln in combined.splitlines() if ln.strip()]
            reason = " | ".join(lines[:4])[:400]
            return {"ok": False, "reason": f"pub resolution failure: {reason}"}
    if proc.returncode != 0:
        return {
            "ok": False,
            "reason": f"pub get exit={proc.returncode}: {combined.strip()[:200]}",
        }
    return {"ok": True, "reason": "pub get clean"}


def _check_node_npm(repo_root: Path) -> Dict[str, object]:
    pkg = repo_root / "package.json"
    if not pkg.exists():
        return {"ok": True, "reason": "no package.json (not a Node/TS repo)"}
    npm_bin = shutil.which("npm")
    if npm_bin is None:
        return {"ok": True, "reason": "npm binary not in PATH (skipped)"}
    # `npm ls --depth=0` is fast (reads node_modules + package.json), reports
    # missing/extraneous deps. Does NOT install. Exit !=0 on tree problems.
    if not (repo_root / "node_modules").exists():
        return {
            "ok": True,
            "reason": "node_modules absent (skipped — would require npm install)",
        }
    try:
        proc = subprocess.run(
            [npm_bin, "ls", "--depth=0", "--silent"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "npm ls timeout (>30s)"}
    if proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        return {
            "ok": False,
            "reason": f"npm ls exit={proc.returncode}: {out.strip()[:300]}",
        }
    return {"ok": True, "reason": "npm ls clean"}


def _check_maven(repo_root: Path) -> Dict[str, object]:
    pom = repo_root / "pom.xml"
    if not pom.exists():
        return {"ok": True, "reason": "no pom.xml (not a Maven repo)"}
    mvn_bin = shutil.which("mvn")
    if mvn_bin is None:
        return {"ok": True, "reason": "mvn binary not in PATH (skipped)"}
    # `mvn -q -o validate` is the fastest goal that fails on missing deps,
    # malformed pom, or unresolvable parent.
    try:
        proc = subprocess.run(
            [mvn_bin, "-q", "-o", "validate"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "mvn validate timeout (>60s)"}
    if proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        # Find canonical Maven failure line.
        failure_lines = [
            ln
            for ln in out.splitlines()
            if "FAILURE" in ln or "ERROR" in ln or "Could not resolve" in ln
        ]
        snippet = " | ".join(failure_lines[:3]) or out.strip()[:300]
        return {"ok": False, "reason": f"mvn validate exit={proc.returncode}: {snippet[:400]}"}
    return {"ok": True, "reason": "mvn validate clean"}


# ---------------------------------------------------------------------------
# Check 2 — broken test detection (static pattern analysis)
# ---------------------------------------------------------------------------
# Fase B evidence: the agent created
#   late PaymentService paymentService;  // declared
#   setUp(() {
#     mockClient = MockSupabaseClient();  // service NEVER assigned
#     ...
#   });
# Any call to `paymentService.createPaymentIntent(...)` would throw
# LateInitializationError. The test file "passed" because pub failed
# first, so the test never actually ran.
#
# Heuristic: for every `late <TypeName> <varName>;` declaration, scan the
# same file for `<varName> =` assignment. If none, flag as broken test.

LATE_DECL = re.compile(r"^\s*late\s+(?:final\s+)?([A-Z]\w*)\s+(\w+)\s*;", re.MULTILINE)
VAR_ASSIGN_TEMPLATE = r"(?<!\.){var_name}\s*="


def check_broken_late_tests(files: List[Path]) -> List[Dict[str, str]]:
    """Return a list of {file, var, type, line} for each late-uninitialized variable.

    Restricted to files whose path contains `test/` or ends `_test.dart`.
    """
    broken: List[Dict[str, str]] = []
    for f in files:
        if f.suffix != ".dart":
            continue
        fstr = str(f)
        if "test/" not in fstr and not fstr.endswith("_test.dart"):
            continue
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for m in LATE_DECL.finditer(content):
            var_type, var_name = m.group(1), m.group(2)
            line_num = content[: m.start()].count("\n") + 1
            assign_pat = re.compile(VAR_ASSIGN_TEMPLATE.format(var_name=re.escape(var_name)))
            # Must find assignment AFTER declaration.
            # Search region after the `late` statement end.
            after = content[m.end() :]
            if not assign_pat.search(after):
                broken.append(
                    {
                        "file": fstr,
                        "variable": var_name,
                        "type": var_type,
                        "line": str(line_num),
                        "reason": "late declaration without assignment in same file",
                    }
                )
    return broken


# ---------------------------------------------------------------------------
# Check 3 — per-file compile / analyze (what V16.2 hook did, kept & improved)
# ---------------------------------------------------------------------------
def _summarize_simple_proc(proc: "subprocess.CompletedProcess[str]", suffix: str) -> Optional[str]:
    """Summarize a per-file checker that signals failure purely via exit code.

    Shared tail for the .py / .sh branches: returns the first non-empty line of
    stderr (falling back to stdout) on failure, or None when the check passed.

    Args:
        proc: Completed process from a py_compile / bash -n invocation.
        suffix: File suffix (used only for the generic fallback message).

    Returns:
        Error summary string on failure, else None.
    """
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return err.splitlines()[0][:300] if err else f"{suffix} check failed"
    return None


def _compile_python(file_path: Path) -> Optional[str]:
    """Byte-compile a Python file via ``py_compile``.

    Args:
        file_path: Path to the ``.py`` file.

    Returns:
        Error summary on failure, else None.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", str(file_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    return _summarize_simple_proc(proc, ".py")


def _compile_shell(file_path: Path) -> Optional[str]:
    """Syntax-check a shell script via ``bash -n``.

    Args:
        file_path: Path to the ``.sh`` file.

    Returns:
        Error summary on failure, else None.
    """
    proc = subprocess.run(
        ["bash", "-n", str(file_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return _summarize_simple_proc(proc, ".sh")


def _compile_dart(file_path: Path) -> Optional[str]:
    """Analyze a single Dart file via ``flutter analyze``.

    Skips (returns None) when no ``pubspec.yaml`` ancestor exists or flutter is
    not on PATH — degrade gracefully rather than report a false error.

    Args:
        file_path: Path to the ``.dart`` file.

    Returns:
        Joined ``error •`` lines on failure, else None.
    """
    project_root = _find_marker_root(file_path, "pubspec.yaml")
    if project_root is None or shutil.which("flutter") is None:
        return None
    proc = subprocess.run(
        ["flutter", "analyze", str(file_path)],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=90,
    )
    if proc.returncode != 0 or "error •" in (proc.stdout or ""):
        errors = [ln for ln in (proc.stdout or "").splitlines() if "error •" in ln][:3]
        if errors:
            return " | ".join(errors)[:300]
    return None


def _compile_java_kotlin(file_path: Path) -> Optional[str]:
    """Compile the enclosing Maven module via ``mvn ... compile``.

    Java/Kotlin per-file compile needs the full classpath, so validate via the
    module's maven ``compile`` goal. Skips when mvn is missing or no ``pom.xml``
    ancestor exists — degrade gracefully.

    Args:
        file_path: Path to the ``.java`` / ``.kt`` file.

    Returns:
        ``mvn compile failed: ...`` summary on failure, else None.
    """
    project_root = _find_marker_root(file_path, "pom.xml")
    if project_root is None or shutil.which("mvn") is None:
        return None
    proc = subprocess.run(
        ["mvn", "-q", "-o", "-DskipTests", "compile"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        errs = [
            ln
            for ln in out.splitlines()
            if "ERROR" in ln or "cannot find symbol" in ln or "incompatible types" in ln
        ]
        snippet = " | ".join(errs[:3]) or out.strip()[:200]
        return f"mvn compile failed: {snippet[:300]}"
    return None


def _compile_ts_js(file_path: Path) -> Optional[str]:
    """Type-check the enclosing TS project via ``npx --no-install tsc``.

    Per-file ``tsc`` requires project context, so run ``tsc -p`` when a
    ``tsconfig.json`` ancestor is found; else skip (no useful per-file check
    without project config). ``npx --no-install`` avoids surprise installs.

    Args:
        file_path: Path to the ``.ts`` / ``.tsx`` / ``.js`` / ``.jsx`` file.

    Returns:
        ``tsc failed: ...`` summary on failure, else None.
    """
    project_root = _find_marker_root(file_path, "tsconfig.json")
    if project_root is None:
        return None  # JS-only project or no TS config — skip
    npx = shutil.which("npx")
    if npx is None:
        return None
    proc = subprocess.run(
        [npx, "--no-install", "tsc", "--noEmit", "-p", "tsconfig.json"],
        cwd=str(project_root),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        errs = [
            ln for ln in out.splitlines() if " error TS" in ln or ln.strip().startswith("error ")
        ]
        snippet = " | ".join(errs[:3]) or out.strip()[:200]
        return f"tsc failed: {snippet[:300]}"
    return None


# Suffix → per-stack compiler. Multiple suffixes can share a handler.
_COMPILE_DISPATCH: Dict[str, "Callable[[Path], Optional[str]]"] = {
    ".py": _compile_python,
    ".sh": _compile_shell,
    ".dart": _compile_dart,
    ".java": _compile_java_kotlin,
    ".kt": _compile_java_kotlin,
    ".ts": _compile_ts_js,
    ".tsx": _compile_ts_js,
    ".js": _compile_ts_js,
    ".jsx": _compile_ts_js,
}


def compile_file(file_path: Path) -> Optional[str]:
    """Return None if file compiles/parses OK, else return error summary.

    H26 multi-stack: covers .py/.sh/.dart/.java/.kt/.ts/.tsx/.jsx/.js.
    Per-file checks where cheap (.py, .sh, single-file dart analyze);
    project-context checks for Java/TS where per-file isn't meaningful.
    Dispatches to a per-stack helper; unknown suffixes are a no-op (None).
    """
    if not file_path.exists():
        return None
    handler = _COMPILE_DISPATCH.get(file_path.suffix.lower())
    if handler is None:
        return None
    try:
        return handler(file_path)
    except subprocess.TimeoutExpired:
        return f"timeout compiling {file_path.name}"
    except Exception as e:  # pragma: no cover — defensive
        return f"exception compiling {file_path.name}: {e!r}"


def _find_marker_root(file_path: Path, marker_name: str) -> Optional[Path]:
    """Walk parents from file_path looking for `marker_name`. Returns dir or None."""
    cur = file_path.parent
    while cur != cur.parent:
        if (cur / marker_name).is_file():
            return cur
        cur = cur.parent
    return None


def _find_flutter_root(file_path: Path) -> Optional[Path]:
    """Backward-compat alias for any caller that imported the old name."""
    return _find_marker_root(file_path, "pubspec.yaml")


# ---------------------------------------------------------------------------
# H35 — Flutter analyze regression detection (whole-root check)
# ---------------------------------------------------------------------------
def check_flutter_analyze(repo_root: Path) -> Dict[str, object]:
    """Run flutter analyze on entire Flutter root to detect regressions.

    Unlike per-file compile_file() checks, this runs analyze on the whole
    project to catch cross-file errors and incremental regressions introduced
    by the agent's changes. Only fires if pubspec.yaml exists and flutter is
    in PATH.

    Returns:
        Dict with keys: "ok" (bool), "reason" (str), "analyze_regression" (bool),
        "error_count" (int), "errors" (list of error lines).
    """
    pubspec = repo_root / "pubspec.yaml"
    if not pubspec.exists():
        return {"ok": True, "reason": "no pubspec.yaml (not a Flutter repo)"}

    flutter_bin = shutil.which("flutter")
    if flutter_bin is None:
        return {"ok": True, "reason": "flutter binary not in PATH (skipped)"}

    try:
        proc = subprocess.run(
            [flutter_bin, "analyze", "--no-pub"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "reason": "flutter analyze timeout (>120s)",
            "analyze_regression": True,
            "error_count": 0,
            "errors": [],
        }
    except Exception as e:
        return {
            "ok": False,
            "reason": f"flutter analyze exception: {e!r}",
            "analyze_regression": False,
            "error_count": 0,
            "errors": [],
        }

    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    # Capture error lines (flutter marks them with "error •" or similar).
    error_lines = [
        ln
        for ln in combined.splitlines()
        if "error •" in ln or "ERROR" in ln or "error:" in ln.lower()
    ]

    if proc.returncode != 0 or error_lines:
        return {
            "ok": False,
            "reason": "flutter analyze found errors",
            "analyze_regression": True,
            "error_count": len(error_lines),
            "errors": error_lines[:10],  # Truncate to 10 lines
        }

    return {
        "ok": True,
        "reason": "flutter analyze clean",
        "analyze_regression": False,
        "error_count": 0,
        "errors": [],
    }


# ---------------------------------------------------------------------------
# H36 — Multi-step test suite verification (per-stack)
# ---------------------------------------------------------------------------
def run_test_suite(repo_root: Path, stack: str) -> Dict[str, object]:
    """Run the repo's test suite to catch accumulated test failures.

    Dispatches by stack:
      - flutter      → `flutter test --no-pub` (timeout 120s)
      - python       → `python -m pytest tests/ -q --tb=no` (timeout 60s)
      - node_ts      → `npm test -- --passWithNoTests` (timeout 60s)
      - prisma_ts    → `npm test -- --passWithNoTests` (timeout 60s)
      - java_spring  → `mvn test -q` (timeout 120s)
      - others       → skipped (no-op success)

    Non-blocking: if timeout occurs, returns {"skipped": "timeout"}.
    Returns summary of last 10-20 lines of output or pass/fail count.
    """
    if stack == "flutter":
        return _run_flutter_tests(repo_root)
    if stack == "python":
        return _run_python_tests(repo_root)
    if stack in ("node_ts", "prisma_ts"):
        return _run_npm_tests(repo_root)
    if stack == "java_spring":
        return _run_maven_tests(repo_root)
    return {"ok": True, "reason": f"no test runner for stack '{stack}'"}


def _run_flutter_tests(repo_root: Path) -> Dict[str, object]:
    """Run flutter test with timeout."""
    flutter_bin = shutil.which("flutter")
    if flutter_bin is None:
        return {"ok": True, "reason": "flutter binary not in PATH (skipped)"}

    try:
        proc = subprocess.run(
            [flutter_bin, "test", "--no-pub"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"skipped": "timeout", "reason": "flutter test >120s"}
    except Exception as e:
        return {"skipped": "exception", "reason": str(e)[:100]}

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = output.splitlines()
    summary = " | ".join(lines[-20:]) if lines else "no output"

    if proc.returncode == 0:
        return {"ok": True, "reason": "tests passed", "output_tail": summary[:500]}
    return {
        "ok": False,
        "reason": f"tests failed (exit={proc.returncode})",
        "output_tail": summary[:500],
    }


def _run_python_tests(repo_root: Path) -> Dict[str, object]:
    """Run pytest with timeout."""
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        return {"ok": True, "reason": "no tests/ directory (skipped)"}

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"skipped": "timeout", "reason": "pytest >60s"}
    except Exception as e:
        return {"skipped": "exception", "reason": str(e)[:100]}

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = output.splitlines()
    summary = " | ".join(lines[-10:]) if lines else "no output"

    if proc.returncode == 0:
        return {"ok": True, "reason": "tests passed", "output_tail": summary[:500]}
    return {
        "ok": False,
        "reason": f"tests failed (exit={proc.returncode})",
        "output_tail": summary[:500],
    }


def _run_npm_tests(repo_root: Path) -> Dict[str, object]:
    """Run npm test with timeout."""
    pkg = repo_root / "package.json"
    if not pkg.exists():
        return {"ok": True, "reason": "no package.json (skipped)"}

    npm_bin = shutil.which("npm")
    if npm_bin is None:
        return {"ok": True, "reason": "npm binary not in PATH (skipped)"}

    try:
        proc = subprocess.run(
            [npm_bin, "test", "--", "--passWithNoTests"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return {"skipped": "timeout", "reason": "npm test >60s"}
    except Exception as e:
        return {"skipped": "exception", "reason": str(e)[:100]}

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = output.splitlines()
    summary = " | ".join(lines[-10:]) if lines else "no output"

    if proc.returncode == 0:
        return {"ok": True, "reason": "tests passed", "output_tail": summary[:500]}
    return {
        "ok": False,
        "reason": f"tests failed (exit={proc.returncode})",
        "output_tail": summary[:500],
    }


def _run_maven_tests(repo_root: Path) -> Dict[str, object]:
    """Run maven test with timeout."""
    pom = repo_root / "pom.xml"
    if not pom.exists():
        return {"ok": True, "reason": "no pom.xml (skipped)"}

    mvn_bin = shutil.which("mvn")
    if mvn_bin is None:
        return {"ok": True, "reason": "mvn binary not in PATH (skipped)"}

    try:
        proc = subprocess.run(
            [mvn_bin, "test", "-q"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return {"skipped": "timeout", "reason": "mvn test >120s"}
    except Exception as e:
        return {"skipped": "exception", "reason": str(e)[:100]}

    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    lines = output.splitlines()
    summary = " | ".join(lines[-10:]) if lines else "no output"

    if proc.returncode == 0:
        return {"ok": True, "reason": "tests passed", "output_tail": summary[:500]}
    return {
        "ok": False,
        "reason": f"tests failed (exit={proc.returncode})",
        "output_tail": summary[:500],
    }


# ---------------------------------------------------------------------------
# H37 — Git vs self-report mismatch detection
# ---------------------------------------------------------------------------
def check_git_vs_report(repo_root: Path) -> Dict[str, object]:
    """Compare git diff vs agent's reported file changes.

    Detects multi-step agents that lose track of their own work (e.g., agent
    reports "skipped 4 files" but git shows 4+ commits). Only runs if .git
    directory exists.

    Returns:
        Dict with keys: "checked" (bool), "reason" (str), "git_changed" (int),
        "mismatch" (bool), "changed_files" (list, first 10).
    """
    git_dir = repo_root / ".git"
    if not git_dir.is_dir():
        return {
            "checked": False,
            "reason": "not a git repository",
            "git_changed": 0,
            "mismatch": False,
            "changed_files": [],
        }

    try:
        # Get files changed in the last commit.
        proc = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return {
            "checked": False,
            "reason": "git diff timeout (>10s)",
            "git_changed": 0,
            "mismatch": False,
            "changed_files": [],
        }
    except Exception as e:
        return {
            "checked": False,
            "reason": f"git diff exception: {e!r}",
            "git_changed": 0,
            "mismatch": False,
            "changed_files": [],
        }

    if proc.returncode != 0:
        return {
            "checked": False,
            "reason": f"git diff failed (exit={proc.returncode})",
            "git_changed": 0,
            "mismatch": False,
            "changed_files": [],
        }

    changed_files = proc.stdout.strip().splitlines() if proc.stdout else []
    return {
        "checked": True,
        "reason": "git diff succeeded",
        "git_changed": len(changed_files),
        "mismatch": False,  # Caller will set to True if needed.
        "changed_files": changed_files[:10],
    }


# ---------------------------------------------------------------------------
# V16.6 W4.3 — Soft-reward integration
# ---------------------------------------------------------------------------
def _update_soft_reward_for_observations(
    result: Dict[str, object],
    outcome: Literal["success", "failure"],
) -> None:
    """Update soft_reward scores for observations mentioned in verification result.

    Called after verification completes. If any observations are referenced
    (e.g., via obs_id in context), update their verify_score based on outcome.

    For now, this is a no-op hook — future work will wire obs_id tracking
    from agent dispatch logs into verifier result to enable targeted updates.

    Args:
        result: Verification result dict from main().
        outcome: "success" if no errors, "failure" if errors found.
    """
    if not _SOFT_REWARD_AVAILABLE or not _SOFT_REWARD_LOOP_AVAILABLE:
        return  # soft_reward modules not loaded; skip

    # V2.0 3c: registrar la señal de reward REAL (resultado de verificación, compile/tests)
    # keyed por sesión. record_reward() auto-crea la tabla reward_signals. Señal HONESTA y
    # no-supervisada — sin labels inventados ni tracking por-obs (que no existe). El loop la
    # consume luego vía soft_reward_loop.compute_adaptation() → ajusta depth params.
    try:
        import os

        sid = os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown"
        _raw_files = result.get("changed_files", []) if isinstance(result, dict) else []
        files: list = _raw_files if isinstance(_raw_files, list) else []
        record_reward(  # type: ignore[reportPossiblyUnbound]  # guarded by _SOFT_REWARD_LOOP_AVAILABLE at line 806
            decision_id=sid,
            reward=1.0 if outcome == "success" else 0.0,
            caller="agent_output_verifier",
            decision_type="agent_verify",
            context=f"files={len(files)}",
        )
    except Exception:
        pass  # fail-open: el reward es advisory, jamás rompe la verificación


# ---------------------------------------------------------------------------
# Main — helpers (cohesive sub-steps extracted to keep main() flat)
# ---------------------------------------------------------------------------
def _collect_files(argv: List[str], repo_root: Path) -> List[Path]:
    """Resolve, de-dupe, and filter the file arguments.

    Keeps only existing files that live inside ``repo_root`` (path-traversal
    guard), preserving argument order and dropping duplicates.

    Args:
        argv: Full process argv (``argv[2:]`` are the file arguments).
        repo_root: Resolved repository root used as the containment boundary.

    Returns:
        Ordered list of existing in-repo files.
    """
    file_args = [Path(p).expanduser().resolve() for p in argv[2:]]
    seen: List[Path] = []
    for f in file_args:
        if f in seen:
            continue
        try:
            f.relative_to(repo_root)
        except ValueError:
            continue
        if f.is_file():
            seen.append(f)
    return seen


def _resolve_stack(repo_root: Path, files: List[Path]) -> str:
    """Detect the repo stack, falling back to the first file's extension.

    H26 multi-stack: detect from marker files (pubspec/pom/package.json). If
    marker detection yields ``generic`` and files were passed, use the first
    file's extension as the discriminator (handles tmpdir test repos lacking
    marker files).

    Args:
        repo_root: Resolved repository root.
        files: Collected in-repo files.

    Returns:
        Detected stack name.
    """
    stack = detect_stack(str(repo_root))
    if stack == "generic" and files:
        stack = detect_stack(str(files[0]))
    return stack


def _run_dependency_check(
    repo_root: Path, files: List[Path], stack: str, errors: List[Dict[str, Any]]
) -> Dict[str, object]:
    """Check 1 — dependency resolution dispatched by stack.

    Only fires if any changed file matches the stack's extensions (avoids
    running mvn for a docs-only commit in a Java repo, etc.). Appends an error
    on failure.

    Args:
        repo_root: Resolved repository root.
        files: Collected in-repo files.
        stack: Detected stack name.
        errors: Mutable error accumulator.

    Returns:
        The dependency-check result dict.
    """
    if not _files_match_stack(files, stack):
        return {"ok": True, "reason": f"no files for stack '{stack}'"}
    dep_result = check_dependency_resolution(repo_root, stack)
    dep_category = {
        "flutter": "pub_resolution",
        "node_ts": "npm_resolution",
        "prisma_ts": "npm_resolution",
        "java_spring": "maven_resolution",
    }.get(stack, "dependency_resolution")
    if not dep_result.get("ok"):
        errors.append(
            {
                "category": dep_category,
                "severity": "error",
                "detail": str(dep_result.get("reason", "")),
            }
        )
    return dep_result


def _run_broken_late_check(files: List[Path], errors: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Check 2 — Dart ``late T var;`` declarations never assigned.

    Flutter-only; equivalent Java/TS patterns require a parser (deferred).
    Appends one error per broken test.

    Args:
        files: Collected in-repo files.
        errors: Mutable error accumulator.

    Returns:
        List of broken-test detail dicts.
    """
    broken_tests = check_broken_late_tests(files)
    for bt in broken_tests:
        errors.append(
            {
                "category": "broken_test_late_uninit",
                "severity": "error",
                "file": bt["file"],
                "variable": bt["variable"],
                "type": bt["type"],
                "line": bt["line"],
                "detail": bt["reason"],
            }
        )
    return broken_tests


def _run_compile_checks(files: List[Path], errors: List[Dict[str, Any]]) -> int:
    """Check 3 — per-file compile/analyze across all collected files.

    Appends a ``compile_error`` per file that fails to compile/parse.

    Args:
        files: Collected in-repo files.
        errors: Mutable error accumulator.

    Returns:
        Count of files that verified cleanly.
    """
    verified_count = 0
    for f in files:
        err = compile_file(f)
        if err is None:
            verified_count += 1
        else:
            errors.append(
                {
                    "category": "compile_error",
                    "severity": "error",
                    "file": str(f),
                    "detail": err,
                }
            )
    return verified_count


def _run_flutter_analyze_check(
    repo_root: Path, files: List[Path], stack: str, errors: List[Dict[str, Any]]
) -> Dict[str, object]:
    """H35 — whole-root ``flutter analyze`` regression check.

    Only runs for Flutter repos (or when any ``.dart`` file is present).
    Appends a regression error on failure.

    Args:
        repo_root: Resolved repository root.
        files: Collected in-repo files.
        stack: Detected stack name.
        errors: Mutable error accumulator.

    Returns:
        The flutter-analyze result dict.
    """
    flutter_analyze_result: Dict[str, object] = {"ok": True, "reason": "not applicable"}
    if stack == "flutter" or any(f.suffix == ".dart" for f in files):
        flutter_analyze_result = check_flutter_analyze(repo_root)
        if not flutter_analyze_result.get("ok"):
            errors.append(
                {
                    "category": "flutter_analyze_regression",
                    "severity": "error",
                    "detail": flutter_analyze_result.get("reason", ""),
                    "error_count": flutter_analyze_result.get("error_count", 0),
                }
            )
    return flutter_analyze_result


def _run_test_suite_check(
    repo_root: Path, stack: str, errors: List[Dict[str, Any]]
) -> Dict[str, object]:
    """H36 — run the repo's test suite to catch accumulated failures.

    A skipped run (timeout/exception) is not treated as an error. Appends a
    ``test_suite_failure`` error only on a real, non-skipped failure.

    Args:
        repo_root: Resolved repository root.
        stack: Detected stack name.
        errors: Mutable error accumulator.

    Returns:
        The test-suite result dict.
    """
    test_suite_result = run_test_suite(repo_root, stack)
    if not test_suite_result.get("ok") and "skipped" not in test_suite_result:
        errors.append(
            {
                "category": "test_suite_failure",
                "severity": "error",
                "detail": test_suite_result.get("reason", ""),
                "output_tail": str(test_suite_result.get("output_tail", ""))[:200],
            }
        )
    return test_suite_result


def _run_git_vs_report_check(
    repo_root: Path, files_reported_count: int, errors: List[Dict[str, Any]]
) -> Dict[str, object]:
    """H37 — compare git changes vs the agent's self-report.

    Flags the case where the agent reported zero files but git shows changes.
    Appends a warning-severity error and mutates the result's ``mismatch`` flag
    when that case is detected.

    Args:
        repo_root: Resolved repository root.
        files_reported_count: Number of files the agent reported.
        errors: Mutable error accumulator.

    Returns:
        The git-vs-report result dict.
    """
    git_result = check_git_vs_report(repo_root)
    _git_raw = git_result.get("git_changed", 0)
    git_changed_count: int = _git_raw if isinstance(_git_raw, int) else 0
    if git_result.get("checked") and git_changed_count > 0 and files_reported_count == 0:
        git_result["mismatch"] = True
        errors.append(
            {
                "category": "git_vs_report_mismatch",
                "severity": "warning",
                "detail": f"Agent reported 0 files but git shows {git_changed_count} changed",
                "git_changed": git_changed_count,
                "files_reported": files_reported_count,
            }
        )
    return git_result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(
            "Usage: agent_output_verifier.py <repo_root> [<file1> <file2> ...]",
            file=sys.stderr,
        )
        return 2

    repo_root = Path(argv[1]).expanduser().resolve()
    if not repo_root.is_dir():
        print(f"repo_root not found: {repo_root}", file=sys.stderr)
        return 2

    files = _collect_files(argv, repo_root)

    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, str]] = []

    stack = _resolve_stack(repo_root, files)

    dep_result = _run_dependency_check(repo_root, files, stack, errors)
    broken_tests = _run_broken_late_check(files, errors)
    verified_count = _run_compile_checks(files, errors)
    flutter_analyze_result = _run_flutter_analyze_check(repo_root, files, stack, errors)
    test_suite_result = _run_test_suite_check(repo_root, stack, errors)
    files_reported_count = len(files)
    git_result = _run_git_vs_report_check(repo_root, files_reported_count, errors)

    result = {
        "repo_root": str(repo_root),
        "stack": stack,
        "files_total": len(files),
        "verified": verified_count,
        "dependency_ok": bool(dep_result.get("ok")),
        "dependency_reason": str(dep_result.get("reason", "")),
        # Backward-compat keys for callers that pre-date H26 multi-stack.
        "pub_ok": bool(dep_result.get("ok")),
        "pub_reason": str(dep_result.get("reason", "")),
        "broken_tests": [bt["file"] + ":" + bt["line"] for bt in broken_tests],
        # H35, H36, H37 new keys
        "flutter_analyze": {
            "ok": flutter_analyze_result.get("ok"),
            "analyze_regression": flutter_analyze_result.get("analyze_regression", False),
            "error_count": flutter_analyze_result.get("error_count", 0),
        },
        "test_suite": {
            "ok": test_suite_result.get("ok"),
            "skipped": "skipped" in test_suite_result,
            "reason": test_suite_result.get("reason", ""),
        },
        "git_vs_report": {
            "checked": git_result.get("checked"),
            "mismatch": git_result.get("mismatch", False),
            "git_changed": git_result.get("git_changed", 0),
            "files_reported": files_reported_count,
        },
        "errors": errors,
        "warnings": warnings,
    }
    # V16.6 W2.1 emit event before print
    emit_event("agent_output_verified", "agent_output_verifier", result=result)
    # V16.6 W4.3: update soft_reward if verification succeeded/failed
    has_errors = bool(errors)
    outcome = "failure" if has_errors else "success"
    _update_soft_reward_for_observations(result, outcome)
    print(json.dumps(result))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
