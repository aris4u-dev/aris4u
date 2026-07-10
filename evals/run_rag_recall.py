#!/usr/bin/env python3
"""Benchmark de recall@k + latencia del substrato vectorial de ARIS4U (WS-D).

Mide el índice semántico REAL (`data/aris_vectors.db`, sqlite-vec + embeddings bge-m3)
con **known-item search**: muestrea N items reales del vec store, deriva una query de
cada uno y comprueba si el KNN recupera ese mismo item en top-k.

Por qué known-item (y no el viejo `rag_recall.jsonl`): aquel dataset apuntaba a docs de
una era purgada (exploitdb/mitre/code-chunks) que ya NO existen en el substrato → habría
medido recall 0 sobre datos inexistentes (teatro). El known-item evalúa el índice tal
como está hoy, sin un dataset curado de relevancia que vuelva a quedar stale.

Dos modos por item:
  - exact   : query = texto completo del item   → sanity del índice (recall@1 ≈ 1.0)
  - partial : query = primeras N palabras       → realista (consulta incompleta)

Requiere Ollama vivo (embeddings) → herramienta de eval LOCAL, no de CI. Degrada con
mensaje claro si el embedder no responde.

Uso:
    .venv312/bin/python evals/run_rag_recall.py [--n 200] [--words 14] [--k 10]
    .venv312/bin/python evals/run_rag_recall.py --json   # salida JSON para tracking
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.v16 import session_manager as sm  # type: ignore[import-not-found]  # noqa: E402
from engine.v16 import vector_store as vs  # type: ignore[import-not-found]  # noqa: E402

_KS = (1, 3, 5, 10)


def _sample_items(n: int) -> list[tuple[str, str]]:
    """Muestra determinista (stride uniforme sobre rowid) de items del vec_map."""
    con = vs._connect()
    try:
        rows = con.execute("SELECT source, source_id FROM vec_map ORDER BY rowid").fetchall()
    finally:
        con.close()
    items = [(r["source"], r["source_id"]) for r in rows]
    if n >= len(items):
        return items
    stride = max(1, len(items) // n)
    return items[::stride][:n]


def _query_from_text(text: str, n_words: int, mode: str) -> str:
    if mode == "exact":
        return text.strip()
    return " ".join(text.split()[:n_words]).strip()


def _rank_strict(hits: list[dict], source: str, source_id: str) -> int | None:
    """Rank 1-based del item EXACTO (source, source_id), o None."""
    for i, h in enumerate(hits, start=1):
        if h["source"] == source and str(h["source_id"]) == str(source_id):
            return i
    return None


def _extract_text(hydrated: object) -> str | None:
    """Extract plain text from _hydrate()'s return value.

    _hydrate() returns Optional[tuple[str, ...]] since V18 Fase E; the first
    element is always the text. Guard for both the new tuple form and the old
    str form so the eval stays compatible across refactors.
    """
    if hydrated is None:
        return None
    if isinstance(hydrated, tuple):
        return hydrated[0] if hydrated and hydrated[0] else None
    # Legacy: plain string
    return str(hydrated) if hydrated else None


def _rank_content(hits: list[dict], target_text: str) -> int | None:
    """Rank 1-based del primer hit cuyo TEXTO == objetivo (descuenta duplicados)."""
    for i, h in enumerate(hits, start=1):
        ht = _extract_text(sm._hydrate(h["source"], h["source_id"]))
        if ht and ht.strip() == target_text.strip():
            return i
    return None


def _eval_mode(items: list[tuple[str, str]], texts: dict, mode: str,
               n_words: int, k: int) -> dict:
    # 'strict' = match por (source,source_id); 'content' = match por texto idéntico
    # (un duplicado de contenido cuenta como acierto → recall real del índice).
    hits_at = {scope: {kk: 0 for kk in _KS} for scope in ("strict", "content")}
    rr_sum = {"strict": 0.0, "content": 0.0}
    latencies: list[float] = []
    evaluated = 0
    for source, sid in items:
        text = texts[(source, sid)]
        query = _query_from_text(text, n_words, mode)
        if not query:
            continue
        t0 = time.perf_counter()
        hits = vs.search(query, k=k)
        latencies.append((time.perf_counter() - t0) * 1000.0)
        evaluated += 1
        for scope, rank in (("strict", _rank_strict(hits, source, sid)),
                            ("content", _rank_content(hits, text))):
            if rank is not None:
                rr_sum[scope] += 1.0 / rank
                for kk in _KS:
                    if rank <= kk:
                        hits_at[scope][kk] += 1
    n = max(evaluated, 1)
    lat = sorted(latencies) or [0.0]
    return {
        "mode": mode,
        "evaluated": evaluated,
        "recall_at_strict": {f"@{kk}": round(hits_at["strict"][kk] / n, 4) for kk in _KS},
        "recall_at_content": {f"@{kk}": round(hits_at["content"][kk] / n, 4) for kk in _KS},
        "mrr_strict": round(rr_sum["strict"] / n, 4),
        "mrr_content": round(rr_sum["content"] / n, 4),
        "latency_ms": {
            "mean": round(statistics.fmean(lat), 2),
            "p50": round(lat[int(len(lat) * 0.50)], 2),
            "p95": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 2),
            "max": round(lat[-1], 2),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ARIS4U RAG recall@k + latency benchmark")
    ap.add_argument("--n", type=int, default=200, help="nº de items a muestrear")
    ap.add_argument("--words", type=int, default=14, help="palabras de la query parcial")
    ap.add_argument("--k", type=int, default=10, help="top-k a recuperar")
    ap.add_argument("--json", action="store_true", help="salida JSON")
    args = ap.parse_args()

    if not vs.available():
        print("[SKIP] vector store no disponible (sqlite-vec/aris_vectors.db).", file=sys.stderr)
        return 2

    # Sanity del embedder: una llamada real. Si falla → Ollama caído.
    if vs._embed("canary probe") is None:
        print("[SKIP] embedder no responde (¿Ollama vivo? mxbai/bge-m3). "
              "Este benchmark es LOCAL, no de CI.", file=sys.stderr)
        return 2

    sampled = _sample_items(args.n)
    # Hidratar textos una sola vez (reutilizados por ambos modos).
    texts: dict = {}
    for source, sid in sampled:
        t = _extract_text(sm._hydrate(source, sid))
        if t and t.strip():
            texts[(source, sid)] = t
    usable = [it for it in sampled if it in texts]

    report = {
        "vectors_total": None,
        "sampled": len(sampled),
        "usable": len(usable),
        "k": args.k,
        "partial_words": args.words,
        "results": [
            _eval_mode(usable, texts, "exact", args.words, args.k),
            _eval_mode(usable, texts, "partial", args.words, args.k),
        ],
    }
    con = vs._connect()
    try:
        report["vectors_total"] = con.execute("SELECT COUNT(*) FROM vec_map").fetchone()[0]
    finally:
        con.close()

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("=== ARIS4U RAG recall benchmark ===")
    print(f"vectores en índice: {report['vectors_total']} | muestreados: {report['sampled']} "
          f"| usables (con texto): {report['usable']} | k={args.k}")
    for r in report["results"]:
        rs, rc, lat = r["recall_at_strict"], r["recall_at_content"], r["latency_ms"]
        print(f"\n[{r['mode']}] evaluados={r['evaluated']}  "
              f"MRR strict={r['mrr_strict']} content={r['mrr_content']}")
        print(f"  recall (strict id)   @1={rs['@1']}  @3={rs['@3']}  @5={rs['@5']}  @10={rs['@10']}")
        print(f"  recall (por contenido)@1={rc['@1']}  @3={rc['@3']}  @5={rc['@5']}  @10={rc['@10']}")
        print(f"  latency mean={lat['mean']}ms  p50={lat['p50']}ms  p95={lat['p95']}ms  max={lat['max']}ms")
    print("\nstrict = match exacto (source,id); content = texto idéntico cuenta (descuenta "
          "duplicados de narrativa). El gap strict<content = redundancia en observations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
