#!/usr/bin/env python3
"""
H32 Log Rotator — Rotate event log on size threshold + phi-guard audit report.

Invoked from session_end.sh (Stop hook). If the active event log exceeds
THRESHOLD_MB (default 50 MB), archive it to logs/archive/ with gzip
compression and create a fresh empty log.  Old archives beyond MAX_FILES
are pruned automatically.

Also provides ``phi_blocks_report()`` — a SQL-queryable summary of
phi_guard / migration-linter block events for EU AI Act Art.12 audit
reporting (F3, Batch F 2026-07-06).

Usage:
    python3 log_rotator.py [--log-file PATH] [--threshold-mb SIZE]
                           [--max-files N] [--phi-report] [--since ISO_TS]

Exit codes:
    0  — rotation / report succeeded (or log is below threshold)
    1  — error during rotation (logged but non-blocking)
"""

import argparse
import gzip
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, UTC
from pathlib import Path


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

_GUARD_EVENTS = frozenset({"phi_to_external_blocked", "migration_lint_blocked"})


def rotate_log(
    log_file: Path,
    threshold_mb: float = 50,
    max_files: int = 5,
) -> int:
    """Rotate log if it exceeds threshold; prune old archives.

    Args:
        log_file: Path to the active JSONL event log.
        threshold_mb: Rotate when log exceeds this size in megabytes
            (default 50 — down from original 500 MB to fix P0 growth).
        max_files: Keep at most this many gzipped archives in logs/archive/;
            oldest files beyond the limit are deleted (default 5).

    Returns:
        0 on success or if no rotation was needed; 1 on error.
    """
    if not log_file.exists():
        return 0

    log_size_mb = log_file.stat().st_size / (1024 * 1024)
    if log_size_mb < threshold_mb:
        return 0

    try:
        archive_dir = log_file.parent / "archive"
        archive_dir.mkdir(exist_ok=True)

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archive_name = f"{log_file.stem}-{timestamp}.jsonl.gz"
        archive_path = archive_dir / archive_name

        with open(log_file, "rb") as f_in:
            with gzip.open(archive_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

        # Truncate active log (keep inode, reset to empty).
        log_file.write_text("")

        print(
            f"H32 log_rotator: rotated {log_file.name} ({log_size_mb:.1f} MB) "
            f"→ {archive_path.name}",
            file=sys.stderr,
        )

        # Prune archives beyond max_files (oldest first by mtime).
        _prune_archives(archive_dir, log_file.stem, max_files)
        return 0

    except Exception as e:
        print(f"H32 log_rotator error: {e!r}", file=sys.stderr)
        return 1


def _prune_archives(archive_dir: Path, stem: str, max_files: int) -> None:
    """Delete oldest archives when count exceeds max_files.

    Args:
        archive_dir: Directory containing ``*.jsonl.gz`` archives.
        stem: Log file stem (e.g. ``v16.1-events``) to scope pruning.
        max_files: Maximum number of archives to keep.
    """
    pattern = f"{stem}-*.jsonl.gz"
    archives = sorted(archive_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    to_delete = archives[: max(0, len(archives) - max_files)]
    for old in to_delete:
        try:
            old.unlink()
            print(f"H32 log_rotator: pruned old archive {old.name}", file=sys.stderr)
        except OSError as e:
            print(f"H32 log_rotator: prune error {old.name}: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Phi-guard audit report
# ---------------------------------------------------------------------------


def phi_blocks_report(
    log_file: Path,
    since: str | None = None,
) -> dict:
    """Count and summarise guard-block events from the JSONL event log.

    Covers two event types (no session_id in their records — known gap):
    - ``phi_to_external_blocked``: PHI detected heading to an external tool.
    - ``migration_lint_blocked``: Migration linter halted a dangerous migration.

    Reads only the active log file.  After rotation, archived events are not
    included (historical counts are captured in the archive).

    Args:
        log_file: Path to the active JSONL log (default: v16.1-events.jsonl).
        since: ISO-8601 timestamp; events before this date are excluded.
            If None, all events in the log are counted.

    Returns:
        dict with keys:
            total           — total guard-block events
            by_type         — Counter {event_type: count}
            by_date         — Counter {YYYY-MM-DD: count}
            phi_patterns    — Counter {matched_pattern: count}  (phi only)
            phi_tools       — Counter {tool_name: count}  (phi only)
    """
    by_type: Counter = Counter()
    by_date: Counter = Counter()
    phi_patterns: Counter = Counter()
    phi_tools: Counter = Counter()

    if not log_file.exists():
        return {
            "total": 0,
            "by_type": dict(by_type),
            "by_date": dict(by_date),
            "phi_patterns": dict(phi_patterns),
            "phi_tools": dict(phi_tools),
        }

    try:
        with log_file.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("event", "")
                if etype not in _GUARD_EVENTS:
                    continue
                ts: str = evt.get("ts", "")
                if since and ts < since:
                    continue
                by_type[etype] += 1
                by_date[ts[:10]] += 1
                if etype == "phi_to_external_blocked":
                    matched = evt.get("matched", "")
                    if matched:
                        phi_patterns[matched] += 1
                    tool = evt.get("tool", "")
                    if tool:
                        phi_tools[tool] += 1
    except OSError as e:
        print(f"phi_blocks_report error: {e}", file=sys.stderr)

    return {
        "total": sum(by_type.values()),
        "by_type": dict(by_type),
        "by_date": dict(sorted(by_date.items())),
        "phi_patterns": dict(phi_patterns.most_common()),
        "phi_tools": dict(phi_tools.most_common()),
    }


def _print_phi_report(log_file: Path, since: str | None) -> int:
    """Print a human-readable phi-guard audit report to stdout.

    Args:
        log_file: Path to the JSONL event log.
        since: Optional ISO-8601 lower-bound filter.

    Returns:
        0 on success.
    """
    report = phi_blocks_report(log_file, since=since)
    print("=== ARIS4U guard-block audit report ===")
    if since:
        print(f"  Since: {since}")
    print(f"  Log  : {log_file}")
    print()
    print(f"  Total guard blocks : {report['total']}")
    print()
    print("  By type:")
    for etype, cnt in report["by_type"].items():
        print(f"    {etype:<35} {cnt:>5}")
    print()
    if report["by_date"]:
        print("  By date (most recent 10):")
        dates = list(report["by_date"].items())[-10:]
        for date, cnt in dates:
            print(f"    {date}  {cnt:>5}")
    print()
    if report["phi_patterns"]:
        print("  Top PHI patterns matched:")
        for pat, cnt in list(report["phi_patterns"].items())[:10]:
            print(f"    {pat:<40} {cnt:>5}")
    print()
    if report["phi_tools"]:
        print("  PHI blocks by tool:")
        for tool, cnt in report["phi_tools"].items():
            print(f"    {tool:<20} {cnt:>5}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    """CLI entry point.

    Returns:
        Exit code (0 = success, 1 = rotation/report error).
    """
    parser = argparse.ArgumentParser(
        description="Rotate event log on size threshold; phi-guard audit report"
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=Path.home() / "projects" / "aris4u" / "logs" / "v16.1-events.jsonl",
        help="Path to the log file",
    )
    parser.add_argument(
        "--threshold-mb",
        type=float,
        default=50,
        help="Rotation threshold in MB (default 50)",
    )
    parser.add_argument(
        "--max-files",
        type=int,
        default=5,
        help="Maximum number of gzipped archives to keep (default 5)",
    )
    parser.add_argument(
        "--phi-report",
        action="store_true",
        help="Print guard-block audit report (no rotation performed)",
    )
    parser.add_argument(
        "--since",
        metavar="ISO_TS",
        default=None,
        help="Filter report events with ts >= ISO_TS",
    )
    args = parser.parse_args()

    if args.phi_report:
        return _print_phi_report(args.log_file, args.since)

    return rotate_log(args.log_file, args.threshold_mb, args.max_files)


if __name__ == "__main__":
    sys.exit(main())
