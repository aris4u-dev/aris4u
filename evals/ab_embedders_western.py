#!/usr/bin/env python3
"""A/B de embedders OCCIDENTALES sobre el substrato real de ARIS4U, segmentado por idioma.

Motiva: el /aris-council (2026-07-01) marcó el swap bge-m3 (China/BAAI) → embedder
occidental como el ÚNICO movimiento capaz de degradar ARIS4U en silencio. El riesgo
concreto (lente Contrarian): bge-m3 es multilingüe real (ES/EN/código) y un sustituto
occidental podría caer en recall ESPAÑOL sin que nada falle. Este harness lo mide
empíricamente ANTES de tocar nada.

Qué hace, y en qué se diferencia de compare_embedders.py:
  - Reusa el mismo known-item recall sobre el corpus REAL (data/aris_vectors.db, ~9.7k items).
  - SEGMENTA el recall por idioma del item objetivo (es / en / otro) → expone el riesgo.
  - Aplica el PREFIJO de tarea correcto por modelo (query vs documento). Sin esto el A/B
    sería injusto: Arctic-Embed y EmbeddingGemma esperan prefijos; bge-m3 no. Comparar sin
    respetarlos penaliza al candidato por implementación, no por calidad.
  - Reporta la DIMENSIÓN de cada modelo → decide el coste de deploy (1024d = drop-in en la
    tabla vec0 float[1024]; 768d = recrear el índice).

NO modifica data/aris_vectors.db. Índice en memoria (numpy, KNN coseno) por modelo.
Corre LOCAL (no CI). Cada modelo debe estar en Ollama (`ollama pull <modelo>`).

Uso:
    .venv312/bin/python evals/ab_embedders_western.py \\
        --models bge-m3 snowflake-arctic-embed2 embeddinggemma --n 300
    .venv312/bin/python evals/ab_embedders_western.py --json > /tmp/ab_embed.json

Recomendación de recursos (M5 48GB, per-agent-model=1): NO cargar los 3 a la vez fuera de
este script. El harness los corre SECUENCIALMENTE y Ollama en lazy los libera entre modelos.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.v16 import session_manager as sm  # noqa: E402
from engine.v16 import vector_store as vs  # noqa: E402

_KS = (1, 3, 5, 10)
_OLLAMA = "http://localhost:11434/api/embeddings"

# Prefijos de tarea por modelo (A/B JUSTO). query = lo que se busca; doc = lo indexado.
# Fuentes: model cards de Snowflake Arctic-Embed 2.0 y Google EmbeddingGemma.
_PREFIX: dict[str, dict[str, str]] = {
    "bge-m3": {"query": "", "doc": ""},
    "mxbai-embed-large": {
        "query": "Represent this sentence for searching relevant passages: ",
        "doc": "",
    },
    "snowflake-arctic-embed2": {"query": "query: ", "doc": ""},
    "embeddinggemma": {
        "query": "task: search result | query: ",
        "doc": "title: none | text: ",
    },
    "granite-embedding": {"query": "", "doc": ""},
}

# Stopwords de alta frecuencia para detección de idioma barata (corpus técnico ES/EN).
_ES = {"que", "de", "la", "el", "en", "con", "para", "una", "por", "los", "las",
       "del", "se", "más", "como", "está", "esto", "cada", "sin", "ser", "hay"}
_EN = {"the", "of", "and", "to", "in", "is", "for", "with", "that", "this", "on",
       "are", "was", "be", "as", "at", "by", "from", "it", "an", "or", "not"}


def _detect_lang(text: str) -> str:
    """Detecta idioma por conteo de stopwords. Devuelve 'es', 'en' u 'other'.

    Args:
        text: Texto a clasificar.

    Returns:
        'es' si predomina español, 'en' si inglés, 'other' si ninguno domina.
    """
    words = [w.strip(".,;:()[]{}\"'`").lower() for w in text.split()]
    es = sum(1 for w in words if w in _ES)
    en = sum(1 for w in words if w in _EN)
    if es == 0 and en == 0:
        return "other"
    if es >= en * 1.2:
        return "es"
    if en >= es * 1.2:
        return "en"
    return "other"


def _embed(model: str, text: str, role: str) -> list[float] | None:
    """Embed con Ollama aplicando el prefijo de tarea del modelo. None si falla.

    Args:
        model: Nombre del modelo en Ollama.
        text: Texto a embeber (se trunca a 2000 chars).
        role: 'query' o 'doc' — selecciona el prefijo correcto.

    Returns:
        Vector de floats, o None si el modelo no responde.
    """
    prefix = _PREFIX.get(model, {}).get(role, "")
    payload = json.dumps({"model": model, "prompt": prefix + text[:2000]}).encode()
    try:
        req = urllib.request.Request(
            _OLLAMA, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as r:  # 120s absorbe carga lazy
            emb = json.loads(r.read()).get("embedding")
        return emb if emb else None
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return None


def _sample(n: int) -> list[tuple[str, str]]:
    """Muestra determinista (stride uniforme) de items del vec_map con su idioma.

    Args:
        n: Número objetivo de items únicos.

    Returns:
        Lista de tuplas (texto, idioma) deduplicada por texto.
    """
    con = vs._connect()
    try:
        rows = con.execute("SELECT source, source_id FROM vec_map ORDER BY rowid").fetchall()
    finally:
        con.close()
    items = [(r["source"], r["source_id"]) for r in rows]
    stride = max(1, len(items) // n)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, sid in items[::stride]:
        raw = sm._hydrate(source, sid)
        # _hydrate devuelve (texto, ...metadata) o un str o None; el texto es el elemento 0.
        text = raw[0] if isinstance(raw, tuple) and raw else raw
        t = text.strip() if isinstance(text, str) else ""
        if t and t not in seen:
            seen.add(t)
            out.append((t, _detect_lang(t)))
        if len(out) >= n:
            break
    return out


def _eval_model(model: str, corpus: list[tuple[str, str]], words: int,
                offset: int = 0) -> dict | None:
    """Indexa el corpus con un modelo y mide known-item recall global y por idioma.

    Args:
        model: Nombre del modelo en Ollama.
        corpus: Lista de (texto, idioma).
        words: Palabras de la query parcial derivada de cada item.

    Returns:
        Dict con recall@k/MRR global + por idioma + latencia + dim, o None si el
        modelo no produjo ningún embedding.
    """
    import numpy as np

    idx_texts: list[str] = []
    idx_langs: list[str] = []
    doc_embs: list[list[float]] = []
    embed_lat: list[float] = []
    failed = 0
    for text, lang in corpus:
        t0 = time.perf_counter()
        e = _embed(model, text, "doc")
        embed_lat.append((time.perf_counter() - t0) * 1000.0)
        if e is None:
            failed += 1
            continue
        idx_texts.append(text)
        idx_langs.append(lang)
        doc_embs.append(e)
    if not doc_embs:
        return None
    mat = np.array(doc_embs, dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9

    # Acumuladores globales y por idioma.
    scopes = ("all", "es", "en", "other")
    hits = {s: {k: 0 for k in _KS} for s in scopes}
    rr = {s: 0.0 for s in scopes}
    cnt = {s: 0 for s in scopes}
    for i, (text, lang) in enumerate(zip(idx_texts, idx_langs)):
        toks = text.split()
        query = " ".join(toks[offset:offset + words]) or " ".join(toks[:words])
        qe = _embed(model, query, "query")
        if qe is None:
            continue
        qv = np.array(qe, dtype=np.float32)
        qv /= np.linalg.norm(qv) + 1e-9
        ranked = list(np.argsort(-(mat @ qv))[: max(_KS)])
        rank = ranked.index(i) + 1 if i in ranked else None
        for s in ("all", lang):
            cnt[s] += 1
            if rank is not None:
                rr[s] += 1.0 / rank
                for k in _KS:
                    if rank <= k:
                        hits[s][k] += 1

    def _pack(s: str) -> dict:
        n = max(cnt[s], 1)
        return {
            "n": cnt[s],
            "recall_at": {f"@{k}": round(hits[s][k] / n, 4) for k in _KS},
            "mrr": round(rr[s] / n, 4),
        }

    lat = sorted(embed_lat) or [0.0]
    return {
        "model": model,
        "dim": len(doc_embs[0]),
        "deploy": "drop-in (1024d)" if len(doc_embs[0]) == 1024 else f"REINDEX ({len(doc_embs[0])}d)",
        "indexed": len(idx_texts),
        "failed_to_index": failed,
        "global": _pack("all"),
        "by_lang": {s: _pack(s) for s in ("es", "en", "other")},
        "embed_latency_ms": {
            "mean": round(statistics.fmean(lat), 1),
            "p95": round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1),
        },
    }


def main() -> int:
    """Punto de entrada CLI del A/B de embedders occidentales."""
    ap = argparse.ArgumentParser(description="A/B de embedders occidentales por idioma")
    ap.add_argument("--models", nargs="+",
                    default=["bge-m3", "snowflake-arctic-embed2", "embeddinggemma"])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--words", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-prefix", action="store_true",
                    help="Modo PRODUCCIÓN: ignora los prefijos de tarea (el path real de "
                         "ARIS4U embed_text NO los aplica). Medición fiel al sistema vivo.")
    ap.add_argument("--query-offset", type=int, default=0,
                    help="Palabra inicial de la query (>0 evita el solape léxico exacto con "
                         "el inicio del documento; reduce el sesgo known-item).")
    args = ap.parse_args()
    if args.no_prefix:  # producción no aplica prefijos → medir así de verdad
        for m in _PREFIX:
            _PREFIX[m] = {"query": "", "doc": ""}

    if not vs.available():
        print("[SKIP] vector store no disponible.", file=sys.stderr)
        return 2

    corpus = _sample(args.n)
    if not corpus:
        print("[SKIP] sin textos en el substrato.", file=sys.stderr)
        return 2
    dist = {s: sum(1 for _, lang in corpus if lang == s) for s in ("es", "en", "other")}

    results = []
    for m in args.models:
        r = _eval_model(m, corpus, args.words, args.query_offset)
        if r is None:
            print(f"[WARN] '{m}' no disponible en Ollama (¿ollama pull {m}?) — omitido.",
                  file=sys.stderr)
            continue
        results.append(r)

    report = {"sampled": len(corpus), "lang_distribution": dist, "results": results}
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print(f"=== A/B embedders occidentales — {len(corpus)} items reales "
          f"(es={dist['es']} en={dist['en']} other={dist['other']}) ===")
    for r in results:
        g = r["global"]["recall_at"]
        es = r["by_lang"]["es"]["recall_at"]
        en = r["by_lang"]["en"]["recall_at"]
        print(f"\n[{r['model']}] dim={r['dim']} → {r['deploy']}  "
              f"indexed={r['indexed']} failed={r['failed_to_index']}")
        print(f"  GLOBAL  recall @1={g['@1']} @5={g['@5']} @10={g['@10']}  MRR={r['global']['mrr']}")
        print(f"  ES({r['by_lang']['es']['n']})  recall @1={es['@1']} @5={es['@5']} @10={es['@10']}  "
              f"MRR={r['by_lang']['es']['mrr']}")
        print(f"  EN({r['by_lang']['en']['n']})  recall @1={en['@1']} @5={en['@5']} @10={en['@10']}  "
              f"MRR={r['by_lang']['en']['mrr']}")
        print(f"  embed latency mean={r['embed_latency_ms']['mean']}ms p95={r['embed_latency_ms']['p95']}ms")
    if len(results) >= 2:
        base = next((r for r in results if r["model"] == "bge-m3"), None)
        best_es = max(results, key=lambda r: r["by_lang"]["es"]["recall_at"]["@5"])
        print(f"\n→ mejor recall@5 ESPAÑOL: {best_es['model']} "
              f"({best_es['by_lang']['es']['recall_at']['@5']})")
        if base:
            print(f"→ baseline bge-m3 recall@5 ES = {base['by_lang']['es']['recall_at']['@5']}  "
                  f"(un candidato solo GANA el swap si iguala o supera esto)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
