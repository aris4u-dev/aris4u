"""
Benchmark for ARIS4U L3 context-compression layer.

Run 1 (baseline):  rates 0.33/0.50/0.67, no force_tokens.
Run 2 (forced):    rates 0.50/0.67, force_tokens=per-payload entities
                   + force_reserve_digit=True.

Both runs use the same 50 payloads (seed=42) from sessions.db.
Results saved to tools/context_compress/BENCHMARK_RESULTS.md.
"""

from __future__ import annotations

import random
import re
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

import requests
import tiktoken
from compress import compress

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).resolve().parents[2] / "data" / "sessions.db"
RESULTS_PATH = Path(__file__).parent / "BENCHMARK_RESULTS.md"
OLLAMA_URL = "http://localhost:11434"
EMBED_MODEL = "bge-m3"
BASELINE_RATES = [0.33, 0.50, 0.67]
FORCE_RATES = [0.50, 0.67]
TARGET_N = 50
MIN_LEN = 200  # chars
RANDOM_SEED = 42  # fixed for reproducible payload selection across both runs
_MODEL_ID = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"

enc = tiktoken.get_encoding("cl100k_base")

# ---------------------------------------------------------------------------
# Entity / fact extraction (regex)
# ---------------------------------------------------------------------------
_ENTITY_RE = re.compile(
    r"(?:"
    r"[A-Z][A-Za-z]{2,}"  # CamelCase / Proper nouns
    r"|[a-z]+(?:_[a-z]+)+"  # snake_case identifiers
    r"|\d{2,}"  # numbers >=2 digits
    r"|/[^\s]+"  # Unix paths
    r")"
)


def extract_entities(text: str) -> set[str]:
    """Return the set of regex-matched entities in *text*."""
    return set(_ENTITY_RE.findall(text))


def fact_retention(original: str, compressed: str) -> float:
    """Fraction of entities from *original* that survive in *compressed*."""
    orig_entities = extract_entities(original)
    if not orig_entities:
        return 1.0
    comp_entities = extract_entities(compressed)
    recalled = orig_entities & comp_entities
    return len(recalled) / len(orig_entities)


# ---------------------------------------------------------------------------
# Semantic similarity via bge-m3 (Ollama)
# ---------------------------------------------------------------------------


def embed(text: str) -> list[float] | None:
    """Return embedding vector from local Ollama bge-m3, or None on failure."""
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embeddings",
            json={"model": EMBED_MODEL, "prompt": text[:8192]},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]
    except Exception:
        return None


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Load payloads from DB
# ---------------------------------------------------------------------------


def _interleave(by_src: dict[str, list[dict]], n: int) -> list[dict]:
    """Round-robin across sources until *n* items are collected."""
    mixed: list[dict] = []
    while sum(len(v) for v in by_src.values()) > 0 and len(mixed) < n:
        for src in list(by_src.keys()):
            if by_src[src]:
                mixed.append(by_src[src].pop(0))
            if len(mixed) >= n:
                break
    return mixed[:n]


def load_payloads(n: int = TARGET_N, seed: int = RANDOM_SEED) -> list[dict[str, Any]]:
    """Load up to *n* text payloads from sessions.db, interleaved across sources.

    Args:
        n: Maximum number of payloads to return.
        seed: Random seed for reproducible selection.

    Returns:
        List of dicts with keys ``id``, ``text``, ``src``.
    """
    con = sqlite3.connect(str(DB_PATH))
    rows: list[dict[str, Any]] = []

    for row in con.execute(
        "SELECT id, decision || ' ' || coalesce(rationale,'') AS text, 'decision' AS src "
        "FROM decisions WHERE length(decision || coalesce(rationale,'')) >= ? "
        "ORDER BY RANDOM() LIMIT ?",
        (MIN_LEN, n),
    ):
        rows.append({"id": row[0], "text": row[1].strip(), "src": row[2]})

    for row in con.execute(
        "SELECT id, summary, 'digest' AS src FROM digests "
        "WHERE length(summary) >= ? ORDER BY RANDOM() LIMIT ?",
        (MIN_LEN, n),
    ):
        rows.append({"id": row[0], "text": row[1].strip(), "src": row[2]})

    for row in con.execute(
        "SELECT id, content, 'observation' AS src FROM observations_local "
        "WHERE length(content) >= ? ORDER BY RANDOM() LIMIT ?",
        (MIN_LEN, n),
    ):
        rows.append({"id": row[0], "text": row[1].strip(), "src": row[2]})

    con.close()

    rng = random.Random(seed)
    rng.shuffle(rows)
    by_src: dict[str, list] = {}
    for r in rows:
        by_src.setdefault(r["src"], []).append(r)
    return _interleave(by_src, n)


# ---------------------------------------------------------------------------
# Per-payload measurement
# ---------------------------------------------------------------------------


def _measure_one(
    text: str,
    emb_orig: list[float] | None,
    rate: float,
    forced: bool = False,
) -> dict[str, Any]:
    """Compress *text* at *rate* and return a dict of metrics.

    Args:
        text: Input text to compress.
        emb_orig: Pre-computed embedding of *text* for semantic similarity,
            or None to skip that metric.
        rate: Fraction of tokens to retain.
        forced: When True, passes per-payload entity list as ``force_tokens``
            and sets ``force_reserve_digit=True``.

    Returns:
        Dict with keys ratio, retention, sem_sim, latency_ms, tok_orig.
    """
    tok_orig = len(enc.encode(text))
    entity_tokens = sorted(extract_entities(text)) if forced else []

    t0 = time.perf_counter()
    comp_text = compress(
        text,
        rate=rate,
        force_tokens=entity_tokens,
        force_reserve_digit=forced,
    )
    latency_ms = (time.perf_counter() - t0) * 1000

    tok_comp = len(enc.encode(comp_text)) if comp_text else 1
    ratio = tok_orig / max(tok_comp, 1)
    retention = fact_retention(text, comp_text)

    sem_sim: float | None = None
    if emb_orig is not None:
        emb_comp = embed(comp_text)
        if emb_comp is not None:
            sem_sim = cosine(emb_orig, emb_comp)

    return {
        "ratio": ratio,
        "retention": retention,
        "sem_sim": sem_sim,
        "latency_ms": latency_ms,
        "tok_orig": tok_orig,
    }


def _collect_results(
    payloads: list[dict[str, Any]],
    rates: list[float],
    forced: bool = False,
) -> dict[float, list[dict[str, Any]]]:
    """Run compression at *rates* for every payload.

    Args:
        payloads: Payload dicts from ``load_payloads``.
        rates: List of retention rates to measure.
        forced: Passed through to ``_measure_one``.

    Returns:
        Dict mapping rate -> list of per-payload metric dicts.
    """
    results: dict[float, list[dict]] = {r: [] for r in rates}
    total_n = len(payloads)
    tag = "forced" if forced else "baseline"

    for idx, payload in enumerate(payloads):
        text = payload["text"]
        tok_orig = len(enc.encode(text))
        if tok_orig < 10:
            continue
        emb_orig = embed(text)

        for rate in rates:
            m = _measure_one(text, emb_orig, rate, forced=forced)
            m["src"] = payload["src"]
            results[rate].append(m)

        if (idx + 1) % 10 == 0:
            print(f"  [{tag}] {idx + 1}/{total_n} done")

    return results


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate_rate(rate: float, data: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute median/p10/p90 statistics for a single *rate* bucket."""
    n = len(data)
    ratios = sorted(d["ratio"] for d in data)
    rets = sorted(d["retention"] for d in data)
    sims = [d["sem_sim"] for d in data if d["sem_sim"] is not None]
    lats = sorted(d["latency_ms"] for d in data)

    return {
        "rate": rate,
        "n": n,
        "ratio_med": statistics.median(ratios),
        "ratio_p10": ratios[int(0.1 * n)],
        "ratio_p90": ratios[int(0.9 * n)],
        "retention_med": statistics.median(rets),
        "retention_p10": rets[int(0.1 * n)],
        "sem_sim_med": statistics.median(sims) if sims else None,
        "latency_med_ms": statistics.median(lats),
        "latency_p90_ms": lats[int(0.9 * n)],
    }


def _build_table(
    results: dict[float, list[dict[str, Any]]],
    label: str = "",
) -> list[dict[str, Any]]:
    """Aggregate all rates into a list of row dicts, printing each as it's built."""
    table_rows = []
    for rate in sorted(results.keys()):
        row = _aggregate_rate(rate, results[rate])
        table_rows.append(row)
        sem = f"{row['sem_sim_med']:.4f}" if row["sem_sim_med"] is not None else "N/A"
        prefix = f"[{label}] " if label else ""
        print(
            f"{prefix}rate={rate:.2f} | ratio_med={row['ratio_med']:.2f}x "
            f"| ret={row['retention_med']:.3f} "
            f"| sem={sem} "
            f"| lat={row['latency_med_ms']:.0f}ms"
        )
    return table_rows


# ---------------------------------------------------------------------------
# Verdict (checks forced table at FORCE_RATES)
# ---------------------------------------------------------------------------


def _verdict_forced(baseline_rows: list[dict[str, Any]], forced_rows: list[dict[str, Any]]) -> str:
    """Return GO / operable-point / redirect-to-L2 verdict.

    GO criteria (forced run): ratio >= 2x AND retention >= 0.95 AND lat_p90 < 500ms.
    """
    go_candidates = [
        r
        for r in forced_rows
        if r["ratio_med"] >= 2.0 and r["retention_med"] >= 0.95 and r["latency_p90_ms"] < 500
    ]
    if go_candidates:
        best = max(go_candidates, key=lambda r: r["ratio_med"])
        return (
            f"GO — operable point at rate={best['rate']:.2f} with force: "
            f"ratio={best['ratio_med']:.2f}x, retention={best['retention_med']:.3f}, "
            f"lat_p90={best['latency_p90_ms']:.0f}ms. "
            f"L3 viable for ARIS4U corpus with entity-forced compression."
        )

    best_forced = max(forced_rows, key=lambda r: r["ratio_med"] * r["retention_med"])
    best_bl = max(baseline_rows, key=lambda r: r["retention_med"])
    return (
        f"NO-GO even with force. Best forced: rate={best_forced['rate']:.2f}, "
        f"ratio={best_forced['ratio_med']:.2f}x, "
        f"retention={best_forced['retention_med']:.3f}, "
        f"lat_p90={best_forced['latency_p90_ms']:.0f}ms. "
        f"Baseline best retention={best_bl['retention_med']:.3f} at rate={best_bl['rate']:.2f}. "
        f"Conclusion: L3 top ~1.5x on identifier-dense text. "
        f"Redirect to L2 (relocation, not deletion) for fact-safe compression on this corpus."
    )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _render_table_md(table_rows: list[dict[str, Any]], caption: str = "") -> str:
    """Render the aggregated metrics as a Markdown table."""
    header = (
        f"{'**' + caption + '**' + chr(10) + chr(10) if caption else ''}"
        "| Rate (retained) | N | Ratio median | Ratio p10-p90 | "
        "Fact-retention median | Fact-ret p10 | Sem-sim median | Lat median ms | Lat p90 ms |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    body = ""
    for r in table_rows:
        sem = f"{r['sem_sim_med']:.4f}" if r["sem_sim_med"] is not None else "N/A"
        body += (
            f"| {r['rate']:.2f} | {r['n']} "
            f"| {r['ratio_med']:.2f}x "
            f"| {r['ratio_p10']:.2f}x - {r['ratio_p90']:.2f}x "
            f"| {r['retention_med']:.3f} "
            f"| {r['retention_p10']:.3f} "
            f"| {sem} "
            f"| {r['latency_med_ms']:.0f} "
            f"| {r['latency_p90_ms']:.0f} |\n"
        )
    return header + body


def _write_report(
    total_n: int,
    src_counts: dict[str, int],
    baseline_rows: list[dict[str, Any]],
    forced_rows: list[dict[str, Any]],
    verdict: str,
) -> None:
    """Write BENCHMARK_RESULTS.md to RESULTS_PATH."""
    src_str = ", ".join(f"{k}={v}" for k, v in sorted(src_counts.items()))
    content = (
        "# ARIS4U L3 Context Compression - Benchmark Results\n\n"
        f"Date: 2026-07-05  |  Model: {_MODEL_ID}  |  Device: MPS\n\n"
        f"Payloads: {total_n} ({src_str})  |  seed={RANDOM_SEED}\n"
        "Token counts via tiktoken cl100k (proxy - Claude tokenizer differs ~30%).\n\n"
        "## Run 1 - Baseline (no entity forcing, rates 0.33/0.50/0.67)\n\n"
        + _render_table_md(baseline_rows)
        + "\n## Run 2 - With force_tokens + force_reserve_digit=True (rates 0.50/0.67)\n\n"
        "Parameters: `force_tokens=sorted(extract_entities(text))` per payload "
        "+ `force_reserve_digit=True` via `compress_prompt`.\n\n"
        + _render_table_md(forced_rows)
        + "\n## Comparative delta (forced vs baseline, same payloads)\n\n"
        + _render_delta_md(baseline_rows, forced_rows)
        + f"\n## Verdict\n\n{verdict}\n\n"
        "## Notes\n\n"
        "- Fact-retention = recall of regex-extracted entities (CamelCase, snake_case, "
        "numbers >=2 digits, Unix paths) from original in compressed output.\n"
        "- Semantic similarity = cosine of bge-m3 embeddings (Ollama local).\n"
        "- GO criteria (forced run): ratio >= 2x AND fact-retention >= 0.95 AND lat_p90 < 500ms.\n"
        "- force_tokens param: per-payload sorted entity list passed to compress_prompt.\n"
        "- force_reserve_digit param: bool passed directly to compress_prompt.\n"
        "- Both runs use seed=42 for identical payload selection.\n"
    )
    RESULTS_PATH.write_text(content, encoding="utf-8")
    print(f"\nResults written to {RESULTS_PATH}")


def _render_delta_md(
    baseline_rows: list[dict[str, Any]],
    forced_rows: list[dict[str, Any]],
) -> str:
    """Render side-by-side delta table for rates present in both runs."""
    base_by_rate = {r["rate"]: r for r in baseline_rows}
    lines = (
        "| Rate | Ratio baseline | Ratio forced | Ret baseline | Ret forced | "
        "Sim baseline | Sim forced | Lat baseline | Lat forced |\n"
        "|---|---|---|---|---|---|---|---|---|\n"
    )
    for row in forced_rows:
        rate = row["rate"]
        b = base_by_rate.get(rate)
        if b is None:
            continue
        b_sim = f"{b['sem_sim_med']:.4f}" if b["sem_sim_med"] is not None else "N/A"
        f_sim = f"{row['sem_sim_med']:.4f}" if row["sem_sim_med"] is not None else "N/A"
        lines += (
            f"| {rate:.2f} "
            f"| {b['ratio_med']:.2f}x | {row['ratio_med']:.2f}x "
            f"| {b['retention_med']:.3f} | {row['retention_med']:.3f} "
            f"| {b_sim} | {f_sim} "
            f"| {b['latency_med_ms']:.0f}ms | {row['latency_med_ms']:.0f}ms |\n"
        )
    return lines


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run() -> None:
    """Orchestrate the full benchmark: baseline + forced runs -> comparison -> report."""
    payloads = load_payloads(TARGET_N, seed=RANDOM_SEED)
    total_n = len(payloads)
    src_counts: dict[str, int] = {}
    for p in payloads:
        src_counts[p["src"]] = src_counts.get(p["src"], 0) + 1
    print(
        f"Loaded {total_n} payloads (seed={RANDOM_SEED}): "
        + ", ".join(f"{k}={v}" for k, v in sorted(src_counts.items()))
    )

    print("Warming up LLMLingua-2 on MPS ...")
    compress("warm up sentence to load model weights into memory.", rate=0.5)
    print("Model warm.")

    print("\n-- Run 1: baseline (0.33 / 0.50 / 0.67) --")
    baseline_results = _collect_results(payloads, BASELINE_RATES, forced=False)
    baseline_rows = _build_table(baseline_results, label="baseline")

    print("\n-- Run 2: forced entities (0.50 / 0.67) --")
    forced_results = _collect_results(payloads, FORCE_RATES, forced=True)
    forced_rows = _build_table(forced_results, label="forced")

    verdict = _verdict_forced(baseline_rows, forced_rows)
    print("\nVERDICT:", verdict)
    _write_report(total_n, src_counts, baseline_rows, forced_rows, verdict)


if __name__ == "__main__":
    run()
