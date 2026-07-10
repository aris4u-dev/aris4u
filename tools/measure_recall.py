#!/usr/bin/env python3
"""Mide la utilidad del auto-recall desde logs/v16.1-events.jsonl.

Puerta de medición de WS-A (shadow): corre ARIS4U_DEPTH_PROTOCOL=0 una semana y
compara contra el período previo. Métrica = recalls con resultados / total (hit-rate)
y promedio de resultados por recall, agrupado por día.

Uso:
    python3 tools/measure_recall.py [--days N]
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "v16.1-events.jsonl"


def parse_days_filter(argv: list[str]) -> int | None:
    """Extract the ``--days N`` value from *argv*.

    Args:
        argv: Argument vector (typically ``sys.argv``).

    Returns:
        The integer following ``--days``, or ``None`` when the flag is absent,
        not followed by a value, or followed by a non-integer.
    """
    if "--days" not in argv:
        return None
    try:
        return int(argv[argv.index("--days") + 1])
    except (ValueError, IndexError):
        return None


def parse_recall_line(line: str) -> dict | None:
    """Parse one log *line* into an auto_recall event, or ``None`` to skip it.

    A line is skipped when it is blank, lacks the ``auto_recall`` substring,
    is not valid JSON, or is not an ``auto_recall`` event.

    Args:
        line: A single raw line from the event log.

    Returns:
        The parsed event dict, or ``None`` if the line is not a usable
        auto_recall event.
    """
    line = line.strip()
    if not line or "auto_recall" not in line:
        return None
    try:
        ev = json.loads(line)
    except Exception:
        return None
    if ev.get("event") != "auto_recall":
        return None
    return ev


def aggregate_events(
    log: Path,
) -> tuple[dict[str, list[int]], dict[str, list[int]], int, int]:
    """Read auto_recall events from *log* and bucket them by day.

    Malformed JSON lines, lines without ``auto_recall``, non-auto_recall events,
    and events without a timestamp are skipped silently.

    Args:
        log: Path to the JSONL event log.

    Returns:
        A 4-tuple ``(by_day, latencies, sem0_total, nsem_known)`` where
        ``by_day`` maps day -> list of result counts, ``latencies`` maps
        day -> list of latency_ms, ``sem0_total`` counts recalls whose
        ``n_semantic`` is 0, and ``nsem_known`` counts recalls that report
        the (forward-only) ``n_semantic`` field at all.
    """
    by_day: dict[str, list[int]] = defaultdict(list)
    latencies: dict[str, list[int]] = defaultdict(list)
    sem0_total = 0  # recalls con n_semantic==0 (lado semántico/Ollama sin aporte)
    nsem_known = 0  # recalls que reportan n_semantic (campo nuevo, forward-only)

    with log.open() as fh:
        for line in fh:
            ev = parse_recall_line(line)
            if ev is None:
                continue
            day = str(ev.get("ts", ""))[:10]
            if not day:
                continue
            by_day[day].append(int(ev.get("results", 0) or 0))
            latencies[day].append(int(ev.get("latency_ms", 0) or 0))
            _nsem = ev.get("n_semantic", None)
            if _nsem is not None:
                nsem_known += 1
                if _nsem == 0:
                    sem0_total += 1

    return by_day, latencies, sem0_total, nsem_known


def select_days(by_day: dict[str, list[int]], days_filter: int | None) -> list[str]:
    """Return sorted days, optionally truncated to the last *days_filter*.

    Args:
        by_day: Mapping of day -> result counts.
        days_filter: If set, keep only the most recent N days.

    Returns:
        Sorted (ascending) list of day strings.
    """
    days = sorted(by_day.keys())
    if days_filter:
        days = days[-days_filter:]
    return days


def print_table(
    days: list[str],
    by_day: dict[str, list[int]],
    latencies: dict[str, list[int]],
) -> tuple[int, int, int]:
    """Print the per-day table and the TOTAL line.

    Args:
        days: Ordered days to print.
        by_day: Mapping of day -> result counts.
        latencies: Mapping of day -> latency_ms values.

    Returns:
        A 3-tuple ``(tot_n, tot_hit, tot_res)`` of total recalls, total recalls
        with at least one hit, and total results across all printed days.
    """
    print(f"{'DÍA':<12} {'recalls':>8} {'con-hit':>8} {'hit-rate':>9} {'avg-res':>8} {'p50-ms':>7}")
    print("-" * 56)
    tot_n = tot_hit = tot_res = 0
    for day in days:
        results = by_day[day]
        n = len(results)
        hits = sum(1 for r in results if r > 0)
        avg = sum(results) / n if n else 0.0
        lat = sorted(latencies[day])
        p50 = lat[len(lat) // 2] if lat else 0
        tot_n += n
        tot_hit += hits
        tot_res += sum(results)
        print(f"{day:<12} {n:>8} {hits:>8} {hits / n * 100:>8.0f}% {avg:>8.1f} {p50:>7}")

    print("-" * 56)
    hr = tot_hit / tot_n * 100 if tot_n else 0
    avgr = tot_res / tot_n if tot_n else 0
    print(f"{'TOTAL':<12} {tot_n:>8} {tot_hit:>8} {hr:>8.0f}% {avgr:>8.1f}")
    return tot_n, tot_hit, tot_res


def print_semantic_diagnostic(sem0_total: int, nsem_known: int) -> None:
    """Print the semantic-side diagnostic line when ``n_semantic`` is known.

    Args:
        sem0_total: Number of recalls whose ``n_semantic`` was 0.
        nsem_known: Number of recalls that reported ``n_semantic``.
    """
    if not nsem_known:
        return
    pct0 = 100 * sem0_total // nsem_known
    print(f"\nDiagnóstico semántico: n_semantic==0 en {sem0_total}/{nsem_known} recalls "
          f"({pct0}%) — el lado semántico (Ollama/embeddings) no aportó; "
          f"el resto vino de FTS/decisiones.")


def main() -> None:
    """Entry point: aggregate the event log and print the recall report."""
    days_filter = parse_days_filter(sys.argv)

    if not LOG.exists():
        print(f"No existe el log: {LOG}")
        return

    by_day, latencies, sem0_total, nsem_known = aggregate_events(LOG)

    if not by_day:
        print("Sin eventos auto_recall en el log todavía.")
        return

    days = select_days(by_day, days_filter)
    print_table(days, by_day, latencies)
    print_semantic_diagnostic(sem0_total, nsem_known)
    print("\nGate WS-A: el hit-rate y avg-res en sombra (DEPTH_PROTOCOL=0) deben ser ≥ baseline.")


if __name__ == "__main__":
    main()
