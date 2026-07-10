#!/usr/bin/env python3
"""CLI: read amplification scores from sessions.db.

Usage:
    .venv312/bin/python tools/amplification_score.py --report

What it measures (Batch O — 2026-07-06, all signals now live):
    score = (recalls_useful + f1_useful + capabilities_adopted + guard_blocks)
            / max(recalls_total + f1_total, 1)   per session

    Live signals (read from JSONL log by session_id):
      recalls_useful/total   — recall_feedback JOIN recall_events (SQL)
      capability_adopted     — 'capability_adopted' events in v16.1-events.jsonl
      total_turns            — 'depth_inject' events (proxy for session depth)
      f1_useful / f1_total   — 'f1_feedback' events (Batch O: session_id added)
      guard_blocks           — 'phi_to_external_blocked' + 'migration_lint_blocked'
                               + 'model_routing_blocked' events (Batch O: session_id
                               added; model_routing_blocked added 2026-07-07 closing
                               the ~/.claude frontier gap)
"""
import argparse
import sqlite3
import sys
from pathlib import Path

_ARIS4U_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ARIS4U_ROOT))

from engine.v16 import session_manager  # noqa: E402


def _global_recall_stats(db: sqlite3.Connection) -> dict:
    """Return global recall_feedback counts (all sessions)."""
    row = db.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(useful), 0) AS useful FROM recall_feedback"
    ).fetchone()
    return {"total": row[0] or 0, "useful": int(row[1] or 0)}


def _bar(pct: float, width: int = 20) -> str:
    """Simple ASCII progress bar for 0–100% values."""
    filled = round(pct / 100 * width)
    return "[" + "#" * filled + "-" * (width - filled) + f"] {pct:.1f}%"


def report(db: sqlite3.Connection) -> int:
    """Print a human-readable amplification score report."""
    # Per-session scores stored at session_end
    rows = db.execute(
        "SELECT session_id, computed_at, recalls_useful, recalls_total, "
        "capabilities_adopted, guard_blocks, score "
        "FROM amplification_scores ORDER BY computed_at DESC LIMIT 20"
    ).fetchall()

    global_rf = _global_recall_stats(db)

    print("=== ARIS4U amplification_score ===")
    print()

    if rows:
        print(f"Per-session scores (last {len(rows)}, newest first):")
        print(f"  {'session_id':<22}  {'computed_at':<20}  recall  cap  guard  score")
        print(f"  {'-'*22}  {'-'*20}  ------  ---  -----  -----")
        for sid, cat, ru, rt, cap, gb, score in rows:
            cat_short = (cat or "?")[:19]
            print(
                f"  {sid:<22}  {cat_short:<20}  {ru:>3}/{rt:<3}  {cap:>3}  {gb:>5}  {score:.3f}"
            )
    else:
        print("  No per-session scores yet.")
        print("  Scores are written at SessionEnd — run a session and close it to populate.")

    print()
    global_total = global_rf["total"]
    global_useful = global_rf["useful"]
    global_pct = 100.0 * global_useful / max(global_total, 1)
    print("=== Global signals (all history, not per-session) ===")
    print(f"  recall_feedback: {global_useful}/{global_total} useful")
    print(f"  {_bar(global_pct)}")
    print()

    print("=== Signal status (Batch O — 2026-07-06, all 5 signals live) ===")
    print("  recalls_useful/total  : LIVE — SQL JOIN recall_events + recall_feedback")
    print("  capability_adopted    : LIVE — capability_adopted events in v16.1-events.jsonl")
    print("  total_turns           : LIVE — depth_inject events proxy (per session_id)")
    print("  f1_labels             : LIVE — f1_feedback events carry session_id (Batch O)")
    print("  guard_blocks          : LIVE — phi/migration_lint_blocked + model_routing_blocked")
    print("                          (all carry session_id; model_routing_blocked added 2026-07-07)")
    print()
    print("  Score = (recalls_useful + f1_useful + capabilities_adopted + guard_blocks)")
    print("        / max(recalls_total + f1_total, 1)")

    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 = success).
    """
    ap = argparse.ArgumentParser(
        description="ARIS4U amplification_score reader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--report",
        action="store_true",
        default=True,
        help="Print amplification score report (default)",
    )
    ap.parse_args(argv)  # validates flags; currently only --report exists

    session_manager.init_db()
    db = session_manager._connect()
    try:
        return report(db)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
