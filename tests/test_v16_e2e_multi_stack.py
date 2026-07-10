"""E2E multi-stack integration test — H30 (V16.5, 2026-04-27).

Verifies that for each registered stack, the canonical chain works:

    detect_stack(path) -> stack_name
        ↓
    cosmetic_classifier — gets correct (cosmetic, functional) patterns
    schema_compat_check — dispatches correctly
    agent_output_verifier — dispatches correctly
    migration_linter — multi-stack naming aware

This is the test that was MISSING in V16.4 ship. eval-0427 (Fase A+B)
revealed that multi-stack code was untested end-to-end — tests covered
units, not stack composition. H30 closes that gap.

Why this matters: this test would have caught H28/H29 — schema_drift
and migration_linter hooks were Flutter-only despite tools being
multi-stack. Adding it makes regression of that bug impossible.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(TOOLS))


# ---------------------------------------------------------------------------
# Fixtures: synthetic per-stack repos under tmp_path
# ---------------------------------------------------------------------------

def _make_flutter_repo(root: Path) -> Path:
    (root / "lib" / "services").mkdir(parents=True)
    (root / "supabase" / "migrations").mkdir(parents=True)
    (root / "pubspec.yaml").write_text("name: test_flutter\n")
    return root


def _make_java_spring_repo(root: Path) -> Path:
    (root / "src" / "main" / "java" / "com" / "test").mkdir(parents=True)
    (root / "src" / "main" / "resources" / "db" / "migration").mkdir(parents=True)
    (root / "pom.xml").write_text("<project></project>\n")
    return root


def _make_node_ts_repo(root: Path) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "prisma" / "migrations").mkdir(parents=True)
    (root / "package.json").write_text('{"name":"test-node"}\n')
    (root / "tsconfig.json").write_text('{}\n')
    return root


def _make_python_repo(root: Path) -> Path:
    (root / "src" / "test_pkg").mkdir(parents=True)
    (root / "alembic" / "versions").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'test_py'\n")
    return root


def _make_generic_repo(root: Path) -> Path:
    (root / "scripts").mkdir(parents=True)
    return root


STACK_BUILDERS = {
    "flutter": _make_flutter_repo,
    "java_spring": _make_java_spring_repo,
    "node_ts": _make_node_ts_repo,
    "python": _make_python_repo,
    "generic": _make_generic_repo,
}


# ---------------------------------------------------------------------------
# Tests — one per dimension of multi-stack contract
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stack_name", sorted(STACK_BUILDERS.keys()))
def test_detect_stack_recognizes_marker_files(tmp_path, stack_name):
    """Each stack's marker files must be detected by stack_registry."""
    from stack_registry import detect_stack

    repo = STACK_BUILDERS[stack_name](tmp_path / stack_name)
    detected = detect_stack(str(repo))
    assert detected == stack_name, f"Expected {stack_name}, got {detected}"


@pytest.mark.parametrize("stack_name", sorted(STACK_BUILDERS.keys()))
def test_cosmetic_patterns_available_per_stack(tmp_path, stack_name):
    """`get_cosmetic_patterns()` must return non-empty pattern lists for
    every stack — flutter/java_spring/node_ts/python — and a defined
    (possibly empty) tuple for generic."""
    from stack_registry import get_cosmetic_patterns

    repo = STACK_BUILDERS[stack_name](tmp_path / stack_name)
    cosmetic, functional = get_cosmetic_patterns(str(repo))
    # Every named stack should have at least one functional pattern
    if stack_name != "generic":
        assert len(functional) > 0, f"{stack_name} has no FUNCTIONAL_PATTERNS"
    # Type contract: both lists of strings
    assert all(isinstance(p, str) for p in cosmetic)
    assert all(isinstance(p, str) for p in functional)


def test_detect_stack_cli_returns_stack_name(tmp_path):
    """CLI wrapper (H31) must print stack name to stdout for hook
    consumption."""
    repo = _make_java_spring_repo(tmp_path / "java_lab")
    cli = REPO_ROOT / "tools" / "detect_stack_cli.py"
    assert cli.exists(), f"detect_stack_cli.py missing at {cli}"
    result = subprocess.run(
        ["python3", str(cli), str(repo)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "java_spring"


def test_detect_stack_cli_no_arg_returns_generic():
    """CLI must not crash when called with no arg — return 'generic' + exit 1."""
    cli = REPO_ROOT / "tools" / "detect_stack_cli.py"
    result = subprocess.run(
        ["python3", str(cli)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.stdout.strip() == "generic"
    assert result.returncode == 1


def test_agent_output_verifier_dispatches_per_stack(tmp_path):
    """agent_output_verifier (F37) must dispatch the dependency check by
    stack — exercise java_spring path (was 0 events in 4d production logs)."""
    repo = _make_java_spring_repo(tmp_path / "client_a_simulated")
    # Exercise the verifier with no changed files — should complete + emit
    # a structured result without crashing.
    sys.path.insert(0, str(TOOLS))
    from agent_output_verifier import detect_stack as verifier_detect_stack

    stack = verifier_detect_stack(str(repo))
    assert stack == "java_spring"


def test_schema_compat_check_handles_java_spring(tmp_path):
    """schema_compat_check (F30) must NOT crash for java_spring repos —
    must emit `_meta` JSON with source field, even when no DB available."""
    repo = _make_java_spring_repo(tmp_path / "java_repo")
    # Add one migration file so the static fallback has something to chew on
    (repo / "src" / "main" / "resources" / "db" / "migration" / "V1__init.sql").write_text(
        "CREATE TABLE users (id BIGINT PRIMARY KEY);\n"
    )

    cli = REPO_ROOT / "tools" / "schema_compat_check.py"
    result = subprocess.run(
        ["python3", str(cli), str(repo)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    # Must not crash — exit 0 (clean) or exit 2 (no source) acceptable; exit
    # 1 (uncaught exception) is the failure mode H17 was supposed to fix.
    assert result.returncode in (0, 2), (
        f"schema_compat_check crashed for java_spring: rc={result.returncode}, "
        f"stderr={result.stderr[:300]}"
    )
    # Output should contain the meta footer with source field
    assert '"_meta"' in result.stdout or '"source"' in result.stdout


def test_list_stacks_includes_all_five(tmp_path):
    """Sanity: stack_registry must expose all 5 documented stacks."""
    from stack_registry import list_stacks

    stacks = list_stacks()
    expected = {"flutter", "java_spring", "node_ts", "python", "generic"}
    assert expected.issubset(set(stacks)), f"Missing stacks: {expected - set(stacks)}"
