#!/usr/bin/env python3
"""Vacuum sessions.db with TTL-policy delete + incremental vacuum.

Per V16.6 ROADMAP W4.4. TTL: digests=14d, gate_results=7d, guards=30d,
decisions=NEVER, engagements=NEVER.

Emits JSONL events to logs/v16.1-events.jsonl for observability.
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Tuple

# Raíz del repo derivada sin hardcode (tools/vacuum_sessions.py → parents[1] = raíz).
_ARIS4U_ROOT = Path(
    os.environ.get("ARIS4U_ROOT")
    or os.environ.get("CLAUDE_PLUGIN_ROOT")
    or Path(__file__).resolve().parents[1]
)
_DEFAULT_DB = _ARIS4U_ROOT / "data" / "sessions.db"


def setup_logging() -> logging.Logger:
    """Setup logging to stdout and events log.

    Args:
        db_path: Path to sessions.db (used to infer log dir).

    Returns:
        Logger instance.
    """
    logger = logging.getLogger("vacuum_sessions")
    logger.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)

    return logger


def emit_event(event_type: str, db_path: Path, data: dict) -> None:
    """Emit JSONL event to logs/v16.1-events.jsonl.

    Args:
        event_type: Type of event (e.g. 'vacuum_delete', 'vacuum_complete').
        db_path: Path to sessions.db (used to infer log dir).
        data: Event payload dict.
    """
    log_dir = db_path.parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    log_file = log_dir / "v16.1-events.jsonl"

    event = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": event_type,
        **data,
    }

    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        logging.warning(f"Failed to emit event: {e}")


def check_schema(conn: sqlite3.Connection) -> bool:
    """Verify sessions.db has required tables and columns.

    Args:
        conn: SQLite connection.

    Returns:
        True if schema is compatible, False otherwise.
    """
    try:
        cursor = conn.cursor()

        # Check required tables exist
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('digests', 'decisions', 'guards', 'gate_results', 'engagements')"
        )
        tables = {row[0] for row in cursor.fetchall()}
        required = {"digests", "decisions", "guards", "gate_results", "engagements"}

        if not required.issubset(tables):
            return False

        # Check digests has created_at column
        cursor.execute("PRAGMA table_info(digests)")
        columns = {row[1] for row in cursor.fetchall()}

        return "created_at" in columns
    except Exception:
        return False


def delete_old_rows(
    conn: sqlite3.Connection, logger: logging.Logger
) -> Tuple[int, int, int]:
    """Delete rows older than TTL policy from sessions.db.

    TTL: digests=14d, gate_results=7d, guards=30d.

    Args:
        conn: SQLite connection (must have IMMEDIATE mode).
        logger: Logger instance.

    Returns:
        Tuple of (digests_deleted, gate_results_deleted, guards_deleted).
    """
    cursor = conn.cursor()

    try:
        # Begin transaction
        cursor.execute("BEGIN IMMEDIATE")

        # DELETE digests older than 14 days
        cursor.execute(
            "DELETE FROM digests WHERE created_at < datetime('now', '-14 days')"
        )
        digests_deleted = cursor.rowcount
        logger.info(f"Deleted {digests_deleted} digests older than 14 days")

        # DELETE gate_results older than 7 days
        cursor.execute(
            "DELETE FROM gate_results WHERE created_at < datetime('now', '-7 days')"
        )
        gate_deleted = cursor.rowcount
        logger.info(f"Deleted {gate_deleted} gate_results older than 7 days")

        # DELETE guards older than 30 days
        cursor.execute(
            "DELETE FROM guards WHERE created_at < datetime('now', '-30 days')"
        )
        guards_deleted = cursor.rowcount
        logger.info(f"Deleted {guards_deleted} guards older than 30 days")

        # NOTE: caller is responsible for commit or rollback
        return digests_deleted, gate_deleted, guards_deleted
    except Exception as e:
        conn.rollback()
        logger.error(f"Transaction failed: {e}")
        raise


def vacuum_incremental(conn: sqlite3.Connection, logger: logging.Logger) -> None:
    """Run incremental vacuum to reclaim freed pages.

    Args:
        conn: SQLite connection.
        logger: Logger instance.
    """
    cursor = conn.cursor()

    try:
        # Enable incremental vacuum mode if not already set
        cursor.execute("PRAGMA auto_vacuum")
        mode = cursor.fetchone()[0]

        if mode == 0:
            logger.info("Setting PRAGMA auto_vacuum=INCREMENTAL")
            cursor.execute("PRAGMA auto_vacuum=INCREMENTAL")

        # Run incremental vacuum (free up to 50 pages per call)
        logger.info("Running PRAGMA incremental_vacuum(50)")
        cursor.execute("PRAGMA incremental_vacuum(50)")

        # Optimize FTS5 indexes
        logger.info("Running PRAGMA optimize")
        cursor.execute("PRAGMA optimize")

        # Checkpoint WAL (truncate if possible)
        logger.info("Running PRAGMA wal_checkpoint(TRUNCATE)")
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        logger.debug("Incremental vacuum and optimization complete")
    except Exception as e:
        logger.error(f"Vacuum failed: {e}")
        raise


def setup_mode(db_path: Path, logger: logging.Logger) -> None:
    """One-time setup: enable incremental auto_vacuum mode.

    Args:
        db_path: Path to sessions.db.
        logger: Logger instance.
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row

        if not check_schema(conn):
            logger.error("Schema check failed")
            sys.exit(1)

        cursor = conn.cursor()
        cursor.execute("PRAGMA auto_vacuum")
        current = cursor.fetchone()[0]

        if current == 0:
            logger.info("Enabling PRAGMA auto_vacuum=INCREMENTAL")
            cursor.execute("PRAGMA auto_vacuum=INCREMENTAL")
            # PRAGMA auto_vacuum only persists to DB header after VACUUM
            cursor.execute("VACUUM")
            conn.commit()
            logger.info("PRAGMA auto_vacuum=INCREMENTAL enabled (persisted via VACUUM)")
        elif current == 2:
            logger.info("PRAGMA auto_vacuum=INCREMENTAL already enabled")
        else:
            logger.warning(f"Unexpected auto_vacuum mode: {current}")

        conn.close()
        emit_event("vacuum_setup", db_path, {"mode": "incremental"})
        logger.info("Setup complete")
    except Exception as e:
        logger.error(f"Setup failed: {e}")
        sys.exit(1)


def delete_mode(
    db_path: Path, logger: logging.Logger, dry_run: bool = False
) -> None:
    """Run TTL-policy delete on sessions.db.

    Args:
        db_path: Path to sessions.db.
        logger: Logger instance.
        dry_run: If True, don't commit deletions.
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row

        if not check_schema(conn):
            logger.error("Schema check failed")
            sys.exit(1)

        logger.info(f"Connected to {db_path}")

        if dry_run:
            logger.info("DRY-RUN mode: no deletions will be committed")

        digests_del, gate_del, guards_del = delete_old_rows(conn, logger)

        if dry_run:
            conn.rollback()
            logger.info("DRY-RUN: rolled back all deletions")
        else:
            conn.commit()
            logger.debug("Transaction committed")

        emit_event(
            "vacuum_delete",
            db_path,
            {
                "digests_deleted": digests_del,
                "gate_results_deleted": gate_del,
                "guards_deleted": guards_del,
                "dry_run": dry_run,
            },
        )

        conn.close()
        logger.info("Delete operation complete")
    except Exception as e:
        logger.error(f"Delete failed: {e}")
        sys.exit(1)


def vacuum_mode(db_path: Path, logger: logging.Logger) -> None:
    """Run incremental vacuum on sessions.db.

    Args:
        db_path: Path to sessions.db.
        logger: Logger instance.
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row

        if not check_schema(conn):
            logger.error("Schema check failed")
            sys.exit(1)

        logger.info(f"Connected to {db_path}")
        vacuum_incremental(conn, logger)

        emit_event("vacuum_incremental", db_path, {"status": "complete"})

        conn.close()
        logger.info("Vacuum operation complete")
    except Exception as e:
        logger.error(f"Vacuum failed: {e}")
        sys.exit(1)


def all_mode(db_path: Path, logger: logging.Logger) -> None:
    """Run complete vacuum cycle: setup + delete + vacuum.

    Args:
        db_path: Path to sessions.db.
        logger: Logger instance.
    """
    logger.info("Running complete vacuum cycle: setup + delete + vacuum")

    # Setup (idempotent)
    try:
        setup_mode(db_path, logger)
    except SystemExit:
        pass  # Log and continue

    # Delete
    delete_mode(db_path, logger, dry_run=False)

    # Vacuum
    vacuum_mode(db_path, logger)

    logger.info("Complete vacuum cycle finished")


def main() -> None:
    """Parse arguments and dispatch to appropriate mode."""
    parser = argparse.ArgumentParser(
        description="Vacuum sessions.db with TTL-policy delete + incremental vacuum."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB,
        help="Path to sessions.db (default: %(default)s)",
    )
    parser.add_argument(
        "--mode",
        choices=["setup", "delete", "vacuum", "all"],
        default="all",
        help="Operation mode (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't commit deletions (delete mode only)",
    )

    args = parser.parse_args()

    logger = setup_logging()

    if not args.db.exists():
        logger.error(f"Database not found: {args.db}")
        sys.exit(1)

    if args.mode == "setup":
        setup_mode(args.db, logger)
    elif args.mode == "delete":
        delete_mode(args.db, logger, dry_run=args.dry_run)
    elif args.mode == "vacuum":
        vacuum_mode(args.db, logger)
    elif args.mode == "all":
        all_mode(args.db, logger)
    else:
        logger.error(f"Unknown mode: {args.mode}")
        sys.exit(1)


if __name__ == "__main__":
    main()
