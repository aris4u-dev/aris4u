"""pytest conftest - ARIS4U test session bootstrap.

Makes archived engine modules importable under their original ``engine.*``
names. Modules that were moved to ``engine/_archive/`` during the session-0401
consolidation are re-registered into ``sys.modules`` so existing tests that
import ``engine.aris4u_dreamer`` etc. continue to work without modification.

Also dynamically resolves and registers the project root on ``sys.path``
using marker-based traversal, making imports robust across IDE, CLI, and
CI/CD execution contexts regardless of the working directory.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Dynamic project-root discovery
# ---------------------------------------------------------------------------


def _find_project_root() -> Path:
    """Locate project root by traversing up from this file.

    Searches ancestor directories for well-known project markers to
    identify the root robustly across IDE, CLI, and CI/CD contexts.

    Returns:
        Path: Resolved absolute path to the project root.

    Raises:
        RuntimeError: If no marker is found within 10 ancestor levels.
    """
    markers = {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        ".git",
        "aris4u_core.py",  # ARIS4U-specific sentinel
    }
    current = Path(__file__).resolve().parent

    for _ in range(10):
        if any((current / m).exists() for m in markers):
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent

    # Fallback: two levels up from conftest.py (tests/ → project root)
    return Path(__file__).resolve().parent.parent


def _ensure_root_on_path(root: Path) -> None:
    """Insert *root* at ``sys.path[0]`` if not already present.

    Idempotent — safe to call multiple times. Placing at index 0 ensures
    local packages shadow any identically-named installed packages.

    Args:
        root: Absolute path to the project root directory.
    """
    root_str = str(root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)


PROJECT_ROOT = _find_project_root()
_ensure_root_on_path(PROJECT_ROOT)

# Global flag: detect if running large test batch (100+ tests)
# Set by session fixture, checked by autouse fixtures to disable cleanup
_DISABLE_FIXTURE_CLEANUP = False
_LARGE_BATCH_EXIT_CODE = None  # Track exit code for large batches


# ---------------------------------------------------------------------------
# Fixtures for self-test suite
# ---------------------------------------------------------------------------


def pytest_configure(config):
    """Called after command line options have been parsed.

    Count tests and disable cleanup for large batches before ANY tests run.
    """
    global _DISABLE_FIXTURE_CLEANUP
    # At configure time, we don't have items yet, so we'll use a different hook


@pytest.fixture(scope="session", autouse=True)
def detect_large_test_load(request):
    """Detect if running a large test batch (100+ tests) and disable cleanup fixtures.

    Large test loads cause pytest finalization hangs due to cumulative resource
    exhaustion. This fixture disables aggressive cleanup for large batches.
    """
    global _DISABLE_FIXTURE_CLEANUP

    # Count total test items
    test_count = len(request.session.items) if hasattr(request, "session") else 0

    if test_count >= 100:
        _DISABLE_FIXTURE_CLEANUP = True
        import sys

        print(
            f"\n[CONFTEST] Detected {test_count} tests. Forcing immediate exit mode.",
            file=sys.stderr,
        )
        sys.stderr.flush()

    yield


def pytest_sessionfinish(session, exitstatus):
    """Report test results. No os._exit — run in batches if hang occurs."""
    pass


@pytest.fixture(scope="session")
def project_root():
    """Return absolute path to project root (portable across CI/dev)."""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def engine_dir(project_root):
    """Return absolute path to engine directory."""
    return project_root / "engine"


@pytest.fixture(scope="session")
def db_connection(db_path):
    """Provide database connection for tests."""
    if not db_path.exists():
        pytest.skip(f"Database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def reset_optimizer_state(request):
    """Reset optimizer singleton state before each test to prevent state pollution.

    Uses the RoutingOptimizer.reset() method to cleanly reset all state:
    - history (routing decisions)
    - failures (failed routing decisions)
    - tier_stats (per-tier statistics)

    Skipped for large test loads (100+ tests) to prevent finalization hang.
    """
    # Check if large batch at setup time
    test_count = len(request.session.items) if hasattr(request, "session") else 0
    if test_count >= 100:
        yield  # Skip all work for large batches
        return

    try:
        from engine.routing_optimizer import optimizer

        optimizer.reset()
    except ImportError:
        pass
    yield
    try:
        from engine.routing_optimizer import optimizer

        optimizer.reset()
    except ImportError:
        pass


@pytest.fixture(autouse=True)
def isolate_sqlite_connections(request):
    """Clean up any lingering SQLite connections between tests.

    Prevents database lock errors and ensures fresh DB initialization.
    Skipped for large test loads (100+ tests) to prevent resource exhaustion.
    """
    import gc
    import sqlite3

    # Check if large batch at setup time
    test_count = len(request.session.items) if hasattr(request, "session") else 0
    if test_count >= 100:
        yield  # Skip all work for large batches
        return

    # Clear any cached connections before test
    gc.collect()

    yield

    # After each test, close any lingering connections
    gc.collect()

    # Close all sqlite connections
    try:
        for conn in list(sqlite3._leaked_connections or []):  # type: ignore[attr-defined]  # private CPython implementation detail for test cleanup
            try:
                conn.close()
            except Exception:
                pass
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# Gate module fixtures (for test_gate.py)
# ---------------------------------------------------------------------------

import tempfile
from typing import Any
from collections.abc import Generator
from unittest import mock


@pytest.fixture
def mock_module() -> Generator[dict[str, Any], None, None]:
    """Create temporary test Python module with all required files.

    Provides:
    - name: Module name (e.g., "auth")
    - path: Path to module file
    - dir: Directory containing module
    - test_dir: Tests directory for the module
    - spec_file: Optional spec.md file

    Returns:
        Dict with module metadata for test use.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)

        # Create module directory structure
        module_name = "test_auth_module"
        module_dir = tmpdir_path / module_name
        module_dir.mkdir()

        # Create main module file
        module_file = module_dir / "module.py"
        module_file.write_text('''"""Test module for gate validation."""

def authenticate(user: str, password: str) -> bool:
    """Authenticate a user.

    Args:
        user: Username
        password: User password

    Returns:
        True if authenticated, False otherwise
    """
    return len(user) > 0 and len(password) > 8


def validate_token(token: str) -> bool:
    """Validate authentication token.

    Args:
        token: JWT token string

    Returns:
        True if valid, False otherwise
    """
    return token.startswith("Bearer ")
''')

        # Create test file
        test_dir = module_dir / "tests"
        test_dir.mkdir()
        test_file = test_dir / "test_module.py"
        test_file.write_text('''"""Tests for test_auth_module."""

import pytest
from module import authenticate, validate_token


def test_authenticate_valid_credentials() -> None:
    """Valid credentials should authenticate."""
    assert authenticate("user", "password123") is True


def test_authenticate_short_password() -> None:
    """Short password should not authenticate."""
    assert authenticate("user", "short") is False


def test_authenticate_empty_user() -> None:
    """Empty user should not authenticate."""
    assert authenticate("", "password123") is False


def test_validate_token_with_bearer() -> None:
    """Token with Bearer prefix should validate."""
    assert validate_token("Bearer token123") is True


def test_validate_token_without_bearer() -> None:
    """Token without Bearer prefix should not validate."""
    assert validate_token("token123") is False


def test_validate_empty_token() -> None:
    """Empty token should not validate."""
    assert validate_token("") is False
''')

        # Create __init__.py files
        (module_dir / "__init__.py").touch()
        (test_dir / "__init__.py").touch()

        yield {
            "name": module_name,
            "path": module_file,
            "dir": module_dir,
            "test_dir": test_dir,
            "module_file": module_file,
        }


@pytest.fixture
def mock_db() -> Generator[dict[str, Any], None, None]:
    """Create temporary sessions.db with gate_results table initialized.

    Initializes:
    - gate_results table with columns: id, module_name, timestamp, status, details, session_ref
    - gate_results_fts virtual table for FTS5 search

    Returns:
        Dict with db_path and connection info for test use.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "sessions.db"

        conn = sqlite3.connect(str(db_path))
        cursor = conn.cursor()

        # Create gate_results table
        cursor.execute("""
            CREATE TABLE gate_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module_name TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                details TEXT,
                e2e_prompt TEXT,
                session_ref TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """)

        # Create FTS5 virtual table for search
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS gate_results_fts USING fts5(
                module_name,
                status,
                details,
                content='gate_results',
                content_rowid='id'
            )
            """)

        # Create trigger to sync FTS5 on insert
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS gate_results_ai AFTER INSERT ON gate_results BEGIN
                INSERT INTO gate_results_fts(rowid, module_name, status, details)
                VALUES (new.id, new.module_name, new.status, new.details);
            END
            """)

        conn.commit()
        conn.close()

        yield {
            "db_path": db_path,
            "connection": None,  # Lazy-load for tests
        }


@pytest.fixture
def mock_subprocess() -> Generator[mock.MagicMock, None, None]:
    """Patch subprocess.run() for controlled test scenarios.

    Allows tests to mock subprocess calls without actually running:
    - python -m py_compile (compile step)
    - pytest (tests step)
    - semgrep (semgrep step)

    Usage:
        mock_subprocess.return_value = mock.MagicMock(
            returncode=0,
            stdout="success"
        )

    Returns:
        MagicMock patching subprocess.run
    """
    with mock.patch("subprocess.run") as mock_run:
        yield mock_run


@pytest.fixture
def temp_module_spec() -> Generator[dict[str, Any], None, None]:
    """Create temporary module spec.md with E2E prompt section.

    Creates:
    - .planning/modules/{name}/spec.md with E2E verification section
    - Module name: "spec_test_module"

    Returns:
        Dict with module name and spec_file path.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create .planning/modules directory structure
        planning_dir = Path(tmpdir) / ".planning" / "modules" / "spec_test_module"
        planning_dir.mkdir(parents=True, exist_ok=True)

        spec_file = planning_dir / "spec.md"
        spec_file.write_text("""# Module Spec: Spec Test Module

## E2E Verification

To verify this module works end-to-end:

1. Open the authentication login page
2. Enter email: test@example.com
3. Enter password: TestPassword123!
4. Click "Log In"
5. Verify you see the dashboard
6. Click "Profile" and verify user data is loaded
7. Log out and verify redirect to login page

Expected: User can complete the full authentication and profile flow without errors.

## Implementation Details

- Uses PostgreSQL for session storage
- JWT tokens expire in 24 hours
- Refresh tokens valid for 7 days
""")

        yield {
            "name": "spec_test_module",
            "spec_file": spec_file,
            "planning_dir": planning_dir,
        }


@pytest.fixture
def mock_gate_result() -> dict[str, Any]:
    """Fixture providing a sample gate result structure.

    Returns a valid gate result JSON structure for testing.

    Returns:
        Dict matching GateResult TypedDict structure.
    """
    return {
        "module": "test_module",
        "status": "PASS",
        "timestamp": "2026-04-21T15:30:45Z",
        "steps": [
            {"name": "compile", "status": "PASS", "details": "Code syntax valid"},
            {"name": "tests", "status": "PASS", "details": "All tests passed (95% coverage)"},
            {"name": "semgrep", "status": "PASS", "details": "No security findings"},
            {"name": "e2e_prompt", "status": "READY", "details": "User can verify module works"},
        ],
        "summary": "Module test_module validation: PASS",
        "e2e_prompt": "To verify this module: 1. Run the test suite 2. Check logs for errors 3. Validate output structure",
    }


@pytest.fixture
def mock_failed_gate_result() -> dict[str, Any]:
    """Fixture providing a failed gate result structure.

    Returns:
        Dict matching GateResult with FAIL status.
    """
    return {
        "module": "bad_module",
        "status": "FAIL",
        "timestamp": "2026-04-21T15:35:20Z",
        "steps": [
            {
                "name": "compile",
                "status": "FAIL",
                "details": "SyntaxError: invalid syntax at line 42",
            },
            {"name": "tests", "status": "FAIL", "details": "Tests blocked by compilation failure"},
            {
                "name": "semgrep",
                "status": "FAIL",
                "details": "Semgrep blocked by compilation failure",
            },
            {"name": "e2e_prompt", "status": "READY", "details": "User can verify module works"},
        ],
        "summary": "Module bad_module validation: FAIL",
        "e2e_prompt": "Cannot verify: module failed compilation. Fix syntax errors and re-run.",
    }


@pytest.fixture
def isolation_tmpdir() -> Generator[Path, None, None]:
    """Provide isolated temporary directory for tests.

    Each test gets its own tmpdir that's cleaned up after test completes.

    Returns:
        Path to temporary directory.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture(autouse=True)
def reset_gate_state() -> Generator[None, None, None]:
    """Reset gate module state between tests.

    Ensures tests don't interfere with each other via shared state.
    """
    yield
    # Cleanup: any module-level state would be reset here
    # Currently gate.py has no module-level state, but this placeholder
    # allows for future state management


@pytest.fixture
def capture_gate_output(capsys: pytest.CaptureFixture) -> Generator[Any, None, None]:
    """Capture stdout/stderr during gate execution.

    Allows tests to verify JSON output to stdout and error messages.

    Returns:
        capsys fixture for accessing captured output.
    """
    yield capsys


@pytest.fixture(autouse=True)
def reset_engine_registry(request):
    """Reset engine singletons between tests to prevent state pollution.

    Clears cached engine instances so each test starts fresh.
    DISABLED for large test loads (100+ tests) to prevent pytest finalization hang.
    """
    # Check if large batch at setup time
    test_count = len(request.session.items) if hasattr(request, "session") else 0
    if test_count >= 100:
        yield  # Skip all work for large batches
        return

    yield
    # Skip cleanup on finalization when many test files loaded (causes hang)
    if hasattr(request.config, "_pytest_session") and getattr(
        request.config, "_pytest_session", None
    ):
        # Safely skip cleanup during session finalization
        return


@pytest.fixture(autouse=True)
def reset_brain_module_state(request):
    """Reset aris4u_brain module-level state between tests.

    The _classifier_loaded and _atom_classifier variables are module globals
    that some tests modify. Reset them after each test.
    Skipped for large test loads to prevent finalization issues.
    """
    # Check if large batch at setup time
    test_count = len(request.session.items) if hasattr(request, "session") else 0
    if test_count >= 100:
        yield  # Skip all work for large batches
        return

    yield

    # After each test, reset brain module state (DO NOT delete module, just reset globals)
    try:
        import engine.aris4u_brain as brain_mod

        brain_mod._classifier_loaded = False
        brain_mod._atom_classifier = None
    except (ImportError, AttributeError):
        pass


@pytest.fixture(autouse=True)
def restore_config_at_test_time(request):
    """Restore real optimized_connect before each test.

    Force cleanup of sys.modules entries and reimport to guarantee fresh unpatched
    functions. Addresses monkeypatch persistence from earlier tests by aggressively
    clearing module cache.

    NOTE: This fixture MUST NOT be skipped for large batches, even though they
    may cause finalization hangs. Without restoration, mocks from earlier tests
    persist and corrupt subsequent test runs.
    """
    # Restore before test runs (ALWAYS, even for large batches)
    # Use reload (not delete) to preserve object identity for test imports
    try:
        import importlib

        import engine.aris4u_brain

        # Reload modules to get fresh (unpatched) functions while preserving identity
        import engine.aris4u_config

        # Reload in correct order: config first, then brain (depends on config)
        importlib.reload(engine.aris4u_config)
        importlib.reload(engine.aris4u_brain)

        # Reset brain state after reload
        engine.aris4u_brain._classifier_loaded = False
        engine.aris4u_brain._atom_classifier = None

    except (ImportError, AttributeError, Exception):
        pass

    yield


@pytest.fixture(autouse=True)
def patch_sessions_db_for_gate_tests(request, mock_db=None):
    """Patch SESSIONS_DB for gate tests that use mock_db fixture.

    When test requests both mock_db and tests gate database persistence,
    patch SESSIONS_DB to point to mock_db["db_path"] so save_gate_result
    writes to the test database.
    """
    # Only apply if the test uses mock_db fixture
    if "mock_db" not in request.fixturenames:
        yield
        return

    # Get the mock_db fixture value
    mock_db = request.getfixturevalue("mock_db")

    try:
        from unittest import mock

        from engine.v16 import config, session_manager

        # Patch SESSIONS_DB in both config and session_manager modules
        with mock.patch.object(config, "SESSIONS_DB", mock_db["db_path"]):
            # Also patch the SESSIONS_DB reference that session_manager imported
            with mock.patch.object(session_manager, "SESSIONS_DB", mock_db["db_path"]):
                yield
    except Exception:
        import traceback

        traceback.print_exc()
        yield


# ---------------------------------------------------------------------------
# V2.0 (2026-06-11): aislamiento del sessions.db REAL.
# Los tests de aris_ingest/save_decision escribían en data/sessions.db en cada
# corrida de suite: 1375 decisions + 1259 guards de basura acumulada (fixture
# "Use JWT RS256 for Client-A auth" x1180). Todo test escribe ahora en un DB tmp.
# Un test que necesite el DB real debe re-monkeypatchear explícitamente.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_sessions_db(tmp_path, monkeypatch):
    """Redirige session_manager.SESSIONS_DB a un DB temporal por test."""
    try:
        from engine.v16 import session_manager
    except ImportError:
        yield
        return
    test_db = tmp_path / "sessions_isolated.db"
    monkeypatch.setattr(session_manager, "SESSIONS_DB", test_db)
    session_manager.init_db()
    yield


@pytest.fixture(autouse=True)
def _isolate_event_log(tmp_path, monkeypatch):
    """Redirige el event log (telemetría auto_recall/model_hint) a un tmp por test.

    Sin esto, los handlers que ejercitan _log_event escriben en el
    logs/v16.1-events.jsonl REAL y contaminan la métrica del freeze
    (freeze_report.py). Forward-only: aísla las corridas futuras.
    """
    monkeypatch.setenv("ARIS4U_EVENTS_LOG", str(tmp_path / "events.jsonl"))
    yield


@pytest.fixture(autouse=True)
def _no_local_generative(request, monkeypatch):
    """RAM-safe: en tests UNITARIOS (no marcados 'integration') evita que el router
    cargue modelos GENERATIVOS locales (p.ej. Foundation-Sec 7.5GB) vía dialectic/digest.
    Hace que _live_models('mac'/'w2') vean 0 modelos -> route_local fail-open (sin carga).
    Los embeddings usan otro endpoint y NO se tocan. Los tests 'integration' SI usan Ollama real.
    """
    if request.node.get_closest_marker("integration"):
        yield
        return
    try:
        from engine.v16 import model_router

        monkeypatch.setattr(model_router, "_query_mac_models", lambda: set())
        monkeypatch.setattr(model_router, "_query_w2_models", lambda: set())
        model_router._model_cache.clear()
    except Exception:
        pass
    yield
