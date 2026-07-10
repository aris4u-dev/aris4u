#!/usr/bin/env python3
"""A/B empírico de modelos de embedding sobre el substrato REAL de ARIS4U.

Responde con datos —no con MTEB de marketing— qué embedder da mejor recall en
ESTE corpus (memoria personal ES/EN, ~9k items). Construye un índice en memoria
(KNN coseno en numpy, sin tocar producción) con una muestra real y corre el mismo
known-item recall de run_rag_recall.py para cada modelo.

Drop-in seguro solo entre modelos de la MISMA dimensión (1024d): bge-m3,
mxbai-embed-large. Un modelo de otra dim (p.ej. qwen3-embedding 4096d) requeriría
truncado MRL + cambio de esquema — fuera de este A/B barato.

Uso:
    .venv312/bin/python evals/compare_embedders.py --models bge-m3 mxbai-embed-large --n 150
    .venv312/bin/python evals/compare_embedders.py --json

Requiere Ollama vivo (cada modelo debe estar disponible con `ollama pull`). LOCAL,
no CI. No modifica data/aris_vectors.db.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.v16 import session_manager as sm  # noqa: E402
from engine.v16 import vector_store as vs  # noqa: E402

_KS = (1, 3, 5, 10)
_OLLAMA = "http://localhost:11434/api/embeddings"


def _embed(model: str, text: str) -> list[float] | None:
    """Embed con un modelo Ollama arbitrario. None si falla."""
    try:
        body = json.dumps({"model": model, "prompt": text[:2000]}).encode()
        req = urllib.request.Request(
            _OLLAMA, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=90) as r:  # 90s absorbe la carga lazy del modelo
            emb = json.loads(r.read()).get("embedding")
        return emb if emb else None
    except Exception:
        return None


def _sample_texts(n: int) -> list[str]:
    """Textos reales del substrato (mismos items que run_rag_recall, deduplicados)."""
    con = vs._connect()
    try:
        rows = con.execute("SELECT source, source_id FROM vec_map ORDER BY rowid").fetchall()
    finally:
        con.close()
    items = [(r["source"], r["source_id"]) for r in rows]
    stride = max(1, len(items) // n)
    out, seen = [], set()
    for source, sid in items[::stride]:
        _hit = sm._hydrate(source, sid)
        # _hydrate returns Optional[tuple[text, ...]] — index 0 is the text string
        t: str | None = _hit[0] if _hit else None
        if t and t.strip() and t.strip() not in seen:
            seen.add(t.strip())
            out.append(t.strip())
        if len(out) >= n:
            break
    return out


def _cosine_knn(matrix, qvec, k: int) -> list[int]:
    """Índices de los k vecinos más cercanos (coseno) — numpy puro."""
    import numpy as np

    sims = matrix @ qvec  # filas ya normalizadas + qvec normalizado → coseno
    return list(np.argsort(-sims)[:k])


def _eval_model(model: str, texts: list[str], words: int, k: int) -> dict | None:
    import numpy as np

    # Indexar: embeber cada texto. Un texto que el modelo NO puede embeber (p.ej.
    # mxbai con context 512 ante un doc largo → HTTP 500) se cuenta como `failed` y
    # se excluye — NO aborta el modelo. Esa tasa de fallo ES un resultado del A/B.
    idx_texts, doc_embs, embed_lat, failed = [], [], [], 0
    for t in texts:
        t0 = time.perf_counter()
        e = _embed(model, t)
        embed_lat.append((time.perf_counter() - t0) * 1000.0)
        if e is None:
            failed += 1
            continue
        idx_texts.append(t)
        doc_embs.append(e)
    if not doc_embs:
        return None  # modelo realmente no disponible
    mat = np.array(doc_embs, dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9

    hits_at = {kk: 0 for kk in _KS}
    rr_sum = 0.0
    for i, t in enumerate(idx_texts):
        query = " ".join(t.split()[:words])
        qe = _embed(model, query)  # la query (corta) casi siempre cabe
        if qe is None:
            continue
        qv = np.array(qe, dtype=np.float32)
        qv /= np.linalg.norm(qv) + 1e-9
        ranked = _cosine_knn(mat, qv, max(_KS))
        if i in ranked:
            rank = ranked.index(i) + 1
            rr_sum += 1.0 / rank
            for kk in _KS:
                if rank <= kk:
                    hits_at[kk] += 1
    n = max(len(idx_texts), 1)
    lat = sorted(embed_lat) or [0.0]
    return {
        "model": model,
        "dim": len(doc_embs[0]),
        "indexed": len(idx_texts),
        "failed_to_index": failed,
        "recall_at": {f"@{kk}": round(hits_at[kk] / n, 4) for kk in _KS},
        "mrr": round(rr_sum / n, 4),
        "embed_latency_ms": {
            "mean": round(statistics.fmean(lat), 1),
            "p50": round(lat[len(lat) // 2], 1),
            "p95": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="A/B de embedders sobre el substrato real")
    ap.add_argument("--models", nargs="+", default=["bge-m3", "mxbai-embed-large"])
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--words", type=int, default=14)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not vs.available():
        print("[SKIP] vector store no disponible.", file=sys.stderr)
        return 2

    texts = _sample_texts(args.n)
    if not texts:
        print("[SKIP] sin textos en el substrato.", file=sys.stderr)
        return 2

    results = []
    for m in args.models:
        r = _eval_model(m, texts, args.words, args.k)
        if r is None:
            print(f"[WARN] modelo '{m}' no disponible en Ollama — omitido.", file=sys.stderr)
            continue
        results.append(r)

    report = {"sampled_unique_texts": len(texts), "partial_words": args.words, "results": results}
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(
        f"=== A/B embedders — {len(texts)} textos reales (query parcial {args.words} palabras) ==="
    )
    for r in results:
        ra, lat = r["recall_at"], r["embed_latency_ms"]
        print(
            f"\n[{r['model']}] dim={r['dim']}  indexed={r['indexed']}  "
            f"failed_to_index={r['failed_to_index']}  MRR={r['mrr']}"
        )
        print(f"  recall @1={ra['@1']}  @3={ra['@3']}  @5={ra['@5']}  @10={ra['@10']}")
        print(f"  embed latency mean={lat['mean']}ms  p50={lat['p50']}ms  p95={lat['p95']}ms")
    if len(results) >= 2:
        best = max(results, key=lambda r: r["recall_at"]["@5"])
        print(f"\n→ mejor recall@5: {best['model']} ({best['recall_at']['@5']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
