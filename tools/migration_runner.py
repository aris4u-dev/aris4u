"""D4 — Standalone CLI migration runner for ARIS4U sessions.db.

Reuses the _migrate_* functions registered in engine.v16.session_manager so
there is no duplicated logic.  Intended for install/upgrade scripts and CI.

Usage:
    python -m tools.migration_runner --init      # create schema + run all pending migrations
    python -m tools.migration_runner --migrate   # run only pending migrations (schema must exist)
    python -m tools.migration_runner --status    # print current user_version and target
"""

from __future__ import annotations

import argparse
import sys


def _get_root():  # noqa: ANN201
    from pathlib import Path

    return Path(__file__).parent.parent


def run_init() -> None:
    """Create the DB schema (if needed) then run all pending migrations."""
    # Ensure the package root is importable when invoked as a standalone script.
    import os

    root = _get_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    os.chdir(str(root))  # ensures SESSIONS_DB relative path resolves correctly

    from engine.v16 import session_manager

    session_manager.init_db()
    import sqlite3

    from engine.v16.config import SESSIONS_DB

    ver = sqlite3.connect(str(SESSIONS_DB)).execute("PRAGMA user_version").fetchone()[0]
    print(f"[migration_runner] init complete — user_version={ver}")


def run_migrate() -> None:
    """Run only pending migrations against an existing DB."""
    import os

    root = _get_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    os.chdir(str(root))

    from engine.v16.config import SESSIONS_DB
    from engine.v16.session_manager import _connect, _run_pending_migrations

    if not SESSIONS_DB.exists():
        print(
            f"[migration_runner] ERROR: {SESSIONS_DB} not found. Run --init first.",
            file=sys.stderr,
        )
        sys.exit(1)

    db = _connect()
    before: int = db.execute("PRAGMA user_version").fetchone()[0]
    _run_pending_migrations(db)
    after: int = db.execute("PRAGMA user_version").fetchone()[0]
    db.close()

    if after == before:
        print(f"[migration_runner] No pending migrations (user_version={after}).")
    else:
        print(f"[migration_runner] Migrated user_version {before} → {after}.")


def run_status() -> None:
    """Print current user_version and the highest registered migration version."""
    import os

    root = _get_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    os.chdir(str(root))

    import sqlite3

    from engine.v16.config import SESSIONS_DB
    from engine.v16.session_manager import _MIGRATION_REGISTRY

    if not SESSIONS_DB.exists():
        print(f"[migration_runner] DB not found at {SESSIONS_DB}")
        return

    current: int = sqlite3.connect(str(SESSIONS_DB)).execute("PRAGMA user_version").fetchone()[0]
    target = max((v for v, _ in _MIGRATION_REGISTRY), default=0)
    pending = [v for v, _ in _MIGRATION_REGISTRY if v > current]
    print(f"[migration_runner] user_version={current}  target={target}  pending={pending}")


def main(argv: list[str] | None = None) -> None:
    """Entry point for CLI invocation."""
    parser = argparse.ArgumentParser(
        prog="python -m tools.migration_runner",
        description="ARIS4U sessions.db schema migration runner.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--init", action="store_true", help="Create schema + run all pending migrations.")
    group.add_argument("--migrate", action="store_true", help="Run only pending migrations (schema must exist).")
    group.add_argument("--status", action="store_true", help="Print current user_version and target.")
    args = parser.parse_args(argv)

    if args.init:
        run_init()
    elif args.migrate:
        run_migrate()
    elif args.status:
        run_status()


if __name__ == "__main__":
    main()
