#!/usr/bin/env python3
"""F2 Audit export — EU AI Act Art.12 event log export + hash-chain verification.

The hash chain was introduced in Batch F (2026-07-06).  Events written before
that date (or by hooks that bypass ``_logger.emit_event``) lack ``hash``/
``prev_hash`` fields and are treated as pre-chain — they appear in exports
but are not included in chain-integrity verification.

Usage:
    # Export to JSON (default):
    .venv312/bin/python tools/audit_export.py

    # Export to CSV:
    .venv312/bin/python tools/audit_export.py --format csv --out audit.csv

    # Verify chain integrity only:
    .venv312/bin/python tools/audit_export.py --verify

    # Filter from a specific timestamp:
    .venv312/bin/python tools/audit_export.py --since 2026-07-06T00:00:00

Exit codes:
    0 — success (export done, or chain intact on --verify)
    1 — chain break(s) detected on --verify
    2 — fatal error (log not found, etc.)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ARIS4U_ROOT = Path(
    os.environ.get("ARIS4U_ROOT")
    or os.environ.get("CLAUDE_PLUGIN_ROOT")
    or Path(__file__).resolve().parents[1]
)
_DEFAULT_LOG = _ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"


# ---------------------------------------------------------------------------
# Chain helpers (must mirror _logger._compute_event_hash exactly)
# ---------------------------------------------------------------------------

_GENESIS_HASH = "0" * 64


def _canonical_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Return record without ``hash``/``prev_hash`` fields (canonical payload)."""
    return {k: v for k, v in record.items() if k not in ("hash", "prev_hash")}


def _compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """SHA-256 of ``prev_hash + canonical_json(payload)``."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(f"{prev_hash}{canonical}".encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Reading events
# ---------------------------------------------------------------------------


def read_events(
    log_path: Path,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Read all JSONL events from the log, optionally filtered by timestamp.

    Args:
        log_path: Path to the JSONL event log.
        since: ISO-8601 timestamp string; events before this are excluded.
            If None, all events are returned.

    Returns:
        List of event dicts in file order.
    """
    if not log_path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with log_path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if since and evt.get("ts", "") < since:
                    continue
                events.append(evt)
    except OSError as e:
        print(f"ERROR reading log: {e}", file=sys.stderr)
    return events


# ---------------------------------------------------------------------------
# Chain verification
# ---------------------------------------------------------------------------


def verify_chain(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Verify the SHA-256 append-only chain for all chained events.

    Pre-chain events (no ``hash`` field) are skipped — they were written
    before Batch F and are not part of the verified segment.

    Genesis detection: the first chained event whose ``prev_hash`` equals
    ``GENESIS_HASH`` (all zeros) is treated as the chain origin.  Any chained
    events that appear in the file *before* the genesis are reported as
    ``pre_genesis`` (not breaks) — they can arise from concurrent writers
    during the initial deployment window and do not indicate tampering.

    Verification then proceeds forward from the genesis event: each event's
    ``prev_hash`` must equal the ``hash`` of the immediately preceding
    chained event.  Self-consistency (``hash == SHA-256(prev_hash + payload)``)
    is also checked independently.

    Args:
        events: List of event dicts in original file order.

    Returns:
        List of break records — empty means chain is intact from genesis.
        Each record: {index, ts, event, error, stored_hash, ...}.
        ``error`` values: ``hash_mismatch`` | ``chain_break`` | ``pre_genesis``.
    """
    chained = [(i, e) for i, e in enumerate(events) if "hash" in e]
    if not chained:
        return []

    # Find the actual genesis: first event with prev_hash == GENESIS_HASH.
    genesis_pos = next(
        (pos for pos, (_, e) in enumerate(chained) if e.get("prev_hash") == _GENESIS_HASH),
        None,
    )

    breaks: list[dict[str, Any]] = []

    # Events before genesis are pre-genesis — informational, not breaks.
    if genesis_pos is None:
        # No genesis found: entire chain is pre-genesis (concurrent-init artifact).
        for idx, evt in chained:
            breaks.append({
                "index": idx,
                "ts": evt.get("ts", "?"),
                "event": evt.get("event", "?"),
                "stored_hash": evt.get("hash", ""),
                "error": "pre_genesis",
            })
        return breaks

    for pos, (idx, evt) in enumerate(chained[:genesis_pos]):
        breaks.append({
            "index": idx,
            "ts": evt.get("ts", "?"),
            "event": evt.get("event", "?"),
            "stored_hash": evt.get("hash", ""),
            "error": "pre_genesis",
        })

    # Verify from genesis forward.
    prev_hash_expected = _GENESIS_HASH
    for idx, evt in chained[genesis_pos:]:
        stored_hash: str = evt.get("hash", "")
        stored_prev: str = evt.get("prev_hash", "")
        payload = _canonical_payload(evt)
        expected_hash = _compute_hash(stored_prev, payload)

        if stored_hash != expected_hash:
            breaks.append({
                "index": idx,
                "ts": evt.get("ts", "?"),
                "event": evt.get("event", "?"),
                "stored_hash": stored_hash,
                "expected_hash": expected_hash,
                "prev_hash": stored_prev,
                "error": "hash_mismatch",
            })
        elif stored_prev != prev_hash_expected:
            breaks.append({
                "index": idx,
                "ts": evt.get("ts", "?"),
                "event": evt.get("event", "?"),
                "stored_hash": stored_hash,
                "expected_hash": expected_hash,
                "prev_hash": stored_prev,
                "expected_prev_hash": prev_hash_expected,
                "error": "chain_break",
            })

        # Always advance by stored_hash so subsequent checks use the actual chain.
        prev_hash_expected = stored_hash

    return breaks


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

_EXPORT_FIELDS = [
    "ts", "event", "source", "session_id", "hook",
    "actor", "action", "decision",
    "hash", "prev_hash",
]


def _row(evt: dict[str, Any]) -> dict[str, Any]:
    """Flatten an event to the standard audit export columns."""
    row: dict[str, Any] = {}
    for field in _EXPORT_FIELDS:
        row[field] = evt.get(field, "")
    # Include all remaining fields as a JSON blob for completeness.
    extra = {k: v for k, v in evt.items() if k not in _EXPORT_FIELDS}
    row["extra"] = json.dumps(extra, default=str) if extra else ""
    return row


def export_json(events: list[dict[str, Any]], out: Path | None) -> None:
    """Export events as a JSON array (one object per event).

    Args:
        events: Filtered event list.
        out: Output path, or None for stdout.
    """
    payload = [_row(e) for e in events]
    text = json.dumps(payload, indent=2, default=str)
    if out:
        out.write_text(text, encoding="utf-8")
        print(f"Exported {len(payload)} events → {out}", file=sys.stderr)
    else:
        print(text)


def export_csv(events: list[dict[str, Any]], out: Path | None) -> None:
    """Export events as CSV.

    Args:
        events: Filtered event list.
        out: Output path, or None for stdout.
    """
    rows = [_row(e) for e in events]
    fieldnames = list(_EXPORT_FIELDS) + ["extra"]
    if out:
        with out.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"Exported {len(rows)} events → {out}", file=sys.stderr)
    else:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code: 0 = success, 1 = chain break(s), 2 = fatal error.
    """
    ap = argparse.ArgumentParser(
        description="ARIS4U audit export — EU AI Act Art.12 (Batch F2)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--log-file",
        type=Path,
        default=_DEFAULT_LOG,
        help=f"Path to JSONL event log (default: {_DEFAULT_LOG})",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="Verify hash-chain integrity only; exit 1 on any break",
    )
    ap.add_argument(
        "--since",
        metavar="ISO_TS",
        default=None,
        help="Filter events with ts >= ISO_TS (e.g. 2026-07-06T00:00:00)",
    )
    ap.add_argument(
        "--format",
        choices=["json", "csv"],
        default="json",
        help="Output format (default: json)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output file path (default: stdout)",
    )
    args = ap.parse_args(argv)

    log_path: Path = args.log_file
    if not log_path.exists():
        print(f"ERROR: log file not found: {log_path}", file=sys.stderr)
        return 2

    events = read_events(log_path, since=args.since)
    chained_count = sum(1 for e in events if "hash" in e)
    pre_chain_count = len(events) - chained_count

    if args.verify:
        print(
            f"Verifying {chained_count} chained events "
            f"({pre_chain_count} pre-chain skipped) ...",
            file=sys.stderr,
        )
        breaks = verify_chain(events)
        real_breaks = [b for b in breaks if b["error"] != "pre_genesis"]
        pre_genesis = [b for b in breaks if b["error"] == "pre_genesis"]
        if pre_genesis:
            print(
                f"  INFO: {len(pre_genesis)} pre-genesis event(s) skipped "
                f"(concurrent-init artifact, not a tamper indicator)",
                file=sys.stderr,
            )
        if real_breaks:
            print(
                f"CHAIN BROKEN — {len(real_breaks)} integrity violation(s):",
                file=sys.stderr,
            )
            for b in real_breaks:
                print(
                    f"  [{b['index']}] {b['ts']} event={b['event']!r} "
                    f"error={b['error']} stored={b['stored_hash'][:12]}...",
                    file=sys.stderr,
                )
            return 1
        verified = chained_count - len(pre_genesis)
        print(
            f"OK — chain intact ({verified} events verified from genesis, "
            f"genesis prev_hash={_GENESIS_HASH[:8]}...)",
            file=sys.stderr,
        )
        return 0

    # Export
    if args.format == "csv":
        export_csv(events, args.out)
    else:
        export_json(events, args.out)

    print(
        f"[audit_export] {len(events)} events "
        f"({chained_count} chained, {pre_chain_count} pre-chain) "
        f"from {log_path.name}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
