#!/usr/bin/env python3
"""Analyze ARIS4U V16.1 validation JSONL log and produce summary stats."""
import json
import statistics
import sys
from collections import Counter, defaultdict


def _load_events(log_path: str) -> list[dict]:
    """Parse a JSONL file, skipping lines that fail to decode.

    Args:
        log_path: Path to the JSONL validation log.

    Returns:
        A list of decoded event dictionaries (malformed lines are dropped).
    """
    events: list[dict] = []
    with open(log_path) as f:
        for line in f:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict):
                continue
            # El JSONL es compartido por varias fuentes: unas emiten 'event', otras
            # 'event_type' (vacuum, nightly). Normalizamos para que los accesos a
            # e['event'] aguas abajo nunca exploten con KeyError.
            e.setdefault("event", e.get("event_type", "unknown"))
            events.append(e)
    return events


def _print_header(log_path: str, events: list[dict]) -> None:
    """Print the report banner and top-level totals.

    Args:
        log_path: Path of the analyzed log (echoed in the report).
        events: The decoded events (non-empty).
    """
    print(f"\n{'='*70}")
    print("ARIS4U V16.1 Validation Log Analysis")
    print(f"{'='*70}")
    print(f"File: {log_path}")
    print(f"Total events: {len(events)}")
    print(f"Timestamp range: {events[0].get('ts', '?')} → {events[-1].get('ts', '?')}")


def _print_event_counts(events: list[dict]) -> None:
    """Print the per-event-type occurrence counts, most common first.

    Args:
        events: The decoded events.
    """
    event_counts = Counter(e['event'] for e in events)
    print(f"\n{'─'*70}")
    print("EVENT COUNTS")
    print(f"{'─'*70}")
    for evt, n in event_counts.most_common():
        print(f"  {evt:25} {n:4d}")


def _print_latency_stats(events: list[dict]) -> None:
    """Print p50/p95/max latency per hook for events carrying ``latency_ms``.

    Args:
        events: The decoded events.
    """
    latencies: defaultdict[str, list] = defaultdict(list)
    for e in events:
        if 'latency_ms' in e:
            latencies[e['event']].append(e['latency_ms'])

    print(f"\n{'─'*70}")
    print("LATENCY STATS (p50/p95/max ms)")
    print(f"{'─'*70}")
    for hook in sorted(latencies.keys()):
        lats = latencies[hook]
        if lats:
            p50 = statistics.median(lats)
            p95 = statistics.quantiles(lats, n=20)[18] if len(lats) >= 20 else max(lats)
            mx = max(lats)
            print(f"  {hook:25} {p50:6.0f} / {p95:6.0f} / {mx:6.0f}")


def _print_f5(events: list[dict]) -> None:
    """Print F5 prevalidation pass/fail/advisory tallies.

    Args:
        events: The decoded events.
    """
    f5_events = [e for e in events if e['event'] == 'f5_prevalidation']
    f5_pass = sum(1 for e in f5_events if e.get('result') == 'pass')
    f5_fail = sum(1 for e in f5_events if e.get('result') == 'fail')
    f5_advisory = sum(1 for e in f5_events if e.get('result') == 'advisory')

    print(f"\n{'─'*70}")
    print("F5 PREVALIDATION")
    print(f"{'─'*70}")
    total_f5 = f5_pass + f5_fail + f5_advisory
    if total_f5 > 0:
        print(f"  Total validations:   {total_f5}")
        print(f"  Pass:                {f5_pass}")
        print(f"  Fail:                {f5_fail}")
        print(f"  Advisory:            {f5_advisory}")
    else:
        print("  No F5 events recorded")


def _print_novelty(events: list[dict]) -> None:
    """Print novelty-detection probe counts (new vs known domains).

    Args:
        events: The decoded events.
    """
    novelty_events = [e for e in events if e['event'] == 'novelty_detection']
    novelty_new = sum(1 for e in novelty_events if e.get('is_new_domain'))

    print(f"\n{'─'*70}")
    print("NOVELTY DETECTION")
    print(f"{'─'*70}")
    if novelty_events:
        print(f"  Total probes:        {len(novelty_events)}")
        print(f"  New domains:         {novelty_new}")
        print(f"  Known domains:       {len(novelty_events) - novelty_new}")
    else:
        print("  No novelty events recorded")


def _print_autotest(events: list[dict]) -> None:
    """Print autotest run totals and pass rate.

    Args:
        events: The decoded events.
    """
    autotest_events = [e for e in events if e['event'] == 'autotest']
    autotest_total = len(autotest_events)
    autotest_pass = sum(1 for e in autotest_events if e.get('failed', 0) == 0)
    autotest_fail = sum(1 for e in autotest_events if e.get('failed', 0) > 0)

    print(f"\n{'─'*70}")
    print("AUTOTEST")
    print(f"{'─'*70}")
    if autotest_total > 0:
        print(f"  Total test runs:     {autotest_total}")
        print(f"  Passed:              {autotest_pass}")
        print(f"  Failed:              {autotest_fail}")
        pct = (autotest_pass / autotest_total * 100) if autotest_total > 0 else 0
        print(f"  Pass rate:           {pct:.1f}%")
    else:
        print("  No autotest events recorded")


def _print_depth_validator(events: list[dict]) -> None:
    """Print the count of depth-validator events.

    Args:
        events: The decoded events.
    """
    depth_val_events = [e for e in events if e['event'] == 'depth_validator']

    print(f"\n{'─'*70}")
    print("DEPTH VALIDATOR")
    print(f"{'─'*70}")
    if depth_val_events:
        print(f"  Total validations:   {len(depth_val_events)}")
    else:
        print("  No depth validator events recorded")


def _print_contract_guard(events: list[dict]) -> None:
    """Print contract-guard check totals (blocked vs allowed).

    Args:
        events: The decoded events.
    """
    contract_events = [e for e in events if e['event'] == 'contract_guard']
    contract_blocked = sum(1 for e in contract_events if not e.get('allowed', True))

    print(f"\n{'─'*70}")
    print("CONTRACT GUARD")
    print(f"{'─'*70}")
    if contract_events:
        print(f"  Total checks:        {len(contract_events)}")
        print(f"  Blocked:             {contract_blocked}")
        print(f"  Allowed:             {len(contract_events) - contract_blocked}")
    else:
        print("  No contract guard events recorded")


def _print_goal_tracking(events: list[dict]) -> None:
    """Print goal-tracking event totals and preserved count.

    Args:
        events: The decoded events.
    """
    goal_events = [
        e for e in events
        if e['event'] in ('goal_checkpoint', 'pre_compact', 'post_compact')
    ]
    goal_preserved = sum(
        1 for e in goal_events if e.get('preserved') or e.get('goal_restored')
    )

    print(f"\n{'─'*70}")
    print("GOAL TRACKING")
    print(f"{'─'*70}")
    if goal_events:
        print(f"  Total goal events:   {len(goal_events)}")
        print(f"  Goals preserved:     {goal_preserved}")
    else:
        print("  No goal tracking events recorded")


def _print_voting(events: list[dict]) -> None:
    """Print voting/consensus totals (approved vs rejected).

    Args:
        events: The decoded events.
    """
    voting_events = [e for e in events if e['event'] == 'voting']
    voting_approved = sum(1 for e in voting_events if e.get('approved'))

    print(f"\n{'─'*70}")
    print("VOTING / CONSENSUS")
    print(f"{'─'*70}")
    if voting_events:
        print(f"  Total votes:         {len(voting_events)}")
        print(f"  Approved:            {voting_approved}")
        print(f"  Rejected:            {len(voting_events) - voting_approved}")
    else:
        print("  No voting events recorded")


def analyze(log_path: str) -> None:
    """Parse a validation JSONL log and print a structured analysis report.

    Args:
        log_path: Path to the JSONL validation log to analyze.

    Returns:
        None. The report is written to stdout; if no events are found a single
        notice line is printed and the function returns early.
    """
    events = _load_events(log_path)

    if not events:
        print(f"No events found in {log_path}")
        return

    _print_header(log_path, events)
    _print_event_counts(events)
    _print_latency_stats(events)
    _print_f5(events)
    _print_novelty(events)
    _print_autotest(events)
    _print_depth_validator(events)
    _print_contract_guard(events)
    _print_goal_tracking(events)
    _print_voting(events)

    print(f"\n{'='*70}\n")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: analyze_validation_log.py <log_file>")
        sys.exit(1)
    analyze(sys.argv[1])
