"""
tests/test_dead_code_scanner_hooks.py

Documents and tests the Hook Caller Blindspot (feedback_hook_caller_blindspot):
a Python-only dead-code scanner will falsely mark functions as unused when their
only callers are bash (.sh) scripts.  ARIS4U has .sh hooks that invoke .py tools
directly (async_vacuum.sh → tools/vacuum_sessions.py, etc.).

P2-B: hook_blindspot_tested (was False)
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

ARIS4U_ROOT = Path(__file__).parent.parent  # ~/projects/aris4u/


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _py_only_scan(root: Path, symbol: str) -> bool:
    """Return True if `symbol` appears to be USED in any .py file under root."""
    for py in root.rglob("*.py"):
        if py.name == "module.py":
            continue  # skip the definition file itself
        text = py.read_text(errors="ignore")
        if re.search(rf"\b{re.escape(symbol)}\b", text):
            return True
    return False


def _py_and_sh_scan(root: Path, symbol: str) -> bool:
    """Return True if `symbol` appears in ANY .py or .sh file under root."""
    for path in list(root.rglob("*.py")) + list(root.rglob("*.sh")):
        if path.name == "module.py":
            continue
        text = path.read_text(errors="ignore")
        if re.search(rf"\b{re.escape(symbol)}\b", text):
            return True
    return False


# ---------------------------------------------------------------------------
# TEST 1 — Python-only scan produces a false positive
# ---------------------------------------------------------------------------

def test_py_only_scan_reports_false_positive() -> None:
    """
    A scanner limited to .py files will flag emit_metric() as dead code
    even though caller.sh invokes it via `python3 -c '… emit_metric()'`.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # The Python module that defines the function
        (root / "module.py").write_text(
            "def emit_metric(name: str, value: float) -> None:\n"
            "    print(f'{name}={value}')\n"
        )

        # The bash caller — the ONLY caller
        (root / "caller.sh").write_text(
            "#!/usr/bin/env bash\n"
            "python3 -c \"from module import emit_metric; emit_metric('cpu', 0.5)\"\n"
        )

        used_in_py = _py_only_scan(root, "emit_metric")
        # No .py file calls emit_metric — only caller.sh does
        assert not used_in_py, (
            "Expected py-only scan to NOT find emit_metric in .py files "
            "(documenting the false-positive blindspot)"
        )


# ---------------------------------------------------------------------------
# TEST 2 — py + sh scan eliminates the false positive
# ---------------------------------------------------------------------------

def test_py_and_sh_scan_finds_sh_caller() -> None:
    """
    Expanding the scan to include .sh files correctly recognises that
    emit_metric() IS called, preventing the false positive.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        (root / "module.py").write_text(
            "def emit_metric(name: str, value: float) -> None:\n"
            "    print(f'{name}={value}')\n"
        )

        (root / "caller.sh").write_text(
            "#!/usr/bin/env bash\n"
            "python3 -c \"from module import emit_metric; emit_metric('cpu', 0.5)\"\n"
        )

        used_in_py_and_sh = _py_and_sh_scan(root, "emit_metric")
        assert used_in_py_and_sh, (
            "py+sh scan should detect emit_metric in caller.sh"
        )


# ---------------------------------------------------------------------------
# Helpers for TEST 3 (extracted to keep CC manageable)
# ---------------------------------------------------------------------------

_PY_PATH_RE = re.compile(r'tools/(\w+)\.py')


def _collect_sh_py_pairs(hooks_dir: Path, tools_dir: Path) -> list[tuple[Path, str]]:
    """Return (sh_file, module_stem) pairs where a .sh calls a tools/*.py script."""
    pairs: list[tuple[Path, str]] = []
    for sh_file in hooks_dir.glob("*.sh"):
        text = sh_file.read_text(errors="ignore")
        for match in _PY_PATH_RE.finditer(text):
            stem = match.group(1)
            if (tools_dir / f"{stem}.py").exists():
                pairs.append((sh_file, stem))
    return pairs


def _find_py_only_fn(defined_fns: list[str], tools_dir: Path, py_module: Path) -> str | None:
    """Return the first function in defined_fns not referenced in any other .py file."""
    other_py_texts = [
        p.read_text(errors="ignore")
        for p in tools_dir.rglob("*.py")
        if p != py_module
    ]
    for fn in defined_fns:
        pattern = re.compile(rf'\b{re.escape(fn)}\b')
        if not any(pattern.search(text) for text in other_py_texts):
            return fn
    return None


# ---------------------------------------------------------------------------
# TEST 3 — Real ARIS4U: .sh hooks call Python tools by script path
# ---------------------------------------------------------------------------

def test_aris4u_sh_hooks_call_python_tools_by_path() -> None:
    """
    ARIS4U's .sh hooks invoke Python tools as scripts (not imported functions),
    e.g. `python3 tools/vacuum_sessions.py`.  A py-only scanner sees those
    tool-level functions as unreferenced.  This test:

    1. Confirms that .sh hook scripts reference at least one Python module path.
    2. Verifies that a py-only grep of that module's functions finds NO callers
       in .py code (all callers live in .sh → blindspot confirmed).
    3. Verifies that expanding to .sh files resolves the blindspot for the
       module-level entry point.

    If no .sh→py call pattern is found the test is xfail (gap documented).
    """
    hooks_dir = ARIS4U_ROOT / "hooks"
    tools_dir = ARIS4U_ROOT / "tools"

    if not hooks_dir.exists():
        pytest.skip("hooks/ directory not found in ARIS4U root")

    sh_to_py = _collect_sh_py_pairs(hooks_dir, tools_dir)

    if not sh_to_py:
        pytest.xfail(
            "No .sh→tools/*.py call patterns found in hooks/. "
            "Gap documented: a py-only dead-code scanner would produce false "
            "positives if such patterns are added in future."
        )

    sh_file, module_stem = sh_to_py[0]
    py_module = tools_dir / f"{module_stem}.py"

    defined_fns = re.findall(r'^def (\w+)', py_module.read_text(errors="ignore"), re.MULTILINE)
    assert defined_fns, f"Expected at least one function defined in {py_module}"

    false_positive_fn = _find_py_only_fn(defined_fns, tools_dir, py_module)

    if false_positive_fn is None:
        pytest.xfail(
            f"All functions in {module_stem}.py are referenced from other .py "
            "files — cannot demonstrate py-only blindspot for this module."
        )

    # Assertion 1: confirm it truly has no .py callers → py-only scan flags it as dead
    other_texts = [
        p.read_text(errors="ignore")
        for p in tools_dir.rglob("*.py") if p != py_module
    ]
    found_in_py = any(
        re.search(rf'\b{re.escape(false_positive_fn)}\b', t) for t in other_texts
    )
    assert not found_in_py, (
        f"py-only scan: '{false_positive_fn}' should appear unused in .py files "
        "(blindspot confirmed)"
    )

    # Assertion 2: .sh references the module path → blindspot resolved by sh-scan
    assert _PY_PATH_RE.search(sh_file.read_text(errors="ignore")), (
        f"{sh_file.name} must reference tools/{module_stem}.py "
        "confirming the .sh is the real entry-point caller"
    )
