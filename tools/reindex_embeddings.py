#!/usr/bin/env python3
"""Reindexa el substrato vectorial de ARIS4U a un nuevo modelo/dimensión, NO destructivo.

Construye un DB nuevo (float[dim] del modelo destino) re-embebiendo los textos reales del
substrato vivo, SIN tocar data/aris_vectors.db. Aplica el prefijo de tarea 'doc' del modelo
destino (EmbeddingGemma/arctic lo exigen). Al terminar corre un smoke de known-item recall
sobre el DB nuevo. El SWAP del archivo es un paso MANUAL y separado (este script nunca borra
ni sustituye el índice vivo) — así el recall en producción (auto_recall) nunca queda a ciegas.

Uso:
    .venv312/bin/python tools/reindex_embeddings.py \\
        --model embeddinggemma --dim 768 --out data/aris_vectors_gemma.db

Tras validar el recall reportado, el swap atómico lo hace el operador:
    mv data/aris_vectors.db data/aris_vectors.bge-m3.bak
    mv data/aris_vectors_gemma.db data/aris_vectors.db
    # y cambiar EMBED_MODEL/EMBED_DIM en engine/v16/config.py
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sqlite3  # noqa: E402

import sqlite_vec  # type: ignore[import-not-found]  # noqa: E402

from engine.v16 import session_manager as sm  # noqa: E402
from engine.v16.config import ARIS_VECTORS_DB, EMBED_PREFIX, OLLAMA_MAC_URL  # noqa: E402

_KS = (1, 5, 10)


def _embed(model: str, text: str, role: str) -> list[float] | None:
    """Embed vía Ollama aplicando el prefijo de tarea del modelo destino."""
    prefix = EMBED_PREFIX.get(model, {}).get(role, "")
    try:
        r = subprocess.run(
            ["curl", "-s", f"{OLLAMA_MAC_URL}/api/embeddings",
             "-d", json.dumps({"model": model, "prompt": prefix + text[:4000]})],
            capture_output=True, text=True, timeout=30,
        )
        return json.loads(r.stdout).get("embedding")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
        return None


def _open_vec(path: Path) -> sqlite3.Connection:
    """Abre una conexión sqlite con la extensión vec cargada."""
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    return con


def _create_schema(con: sqlite3.Connection, dim: int) -> None:
    """Crea la tabla vec0 + el side-map en el DB destino."""
    con.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0("
        f"embedding float[{dim}] distance_metric=cosine, client_id text)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS vec_map (rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
        "source TEXT NOT NULL, source_id TEXT NOT NULL, client_id TEXT NOT NULL DEFAULT '', "
        "item_type TEXT DEFAULT '', content_hash TEXT, indexed_at INTEGER, "
        "UNIQUE(source, source_id))"
    )
    con.commit()


def _reindex(model: str, dim: int, out: Path) -> dict:
    """Re-embebe todo el substrato vivo en un DB nuevo. Devuelve stats."""
    src = _open_vec(Path(str(ARIS_VECTORS_DB)))
    rows = src.execute(
        "SELECT source, source_id, client_id, item_type FROM vec_map ORDER BY rowid"
    ).fetchall()
    src.close()
    total = len(rows)
    print(f"[reindex] {total} items del substrato vivo → {model} ({dim}d) en {out.name}", flush=True)

    if out.exists():
        out.unlink()  # empezar limpio (es un DB temporal nuestro, no el vivo)
    dst = _open_vec(out)
    _create_schema(dst, dim)

    indexed = failed = dim_mismatch = 0
    t0 = time.perf_counter()
    for i, r in enumerate(rows):
        raw = sm._hydrate(r["source"], r["source_id"])
        text = raw[0] if isinstance(raw, tuple) and raw else raw
        if not isinstance(text, str) or not text.strip():
            failed += 1
            continue
        emb = _embed(model, sm._humanize_embedding_text(text), "doc")
        if not emb:
            failed += 1
            continue
        if len(emb) != dim:
            dim_mismatch += 1
            continue
        cur = dst.execute(
            "INSERT INTO vec_map(source, source_id, client_id, item_type, content_hash, "
            "indexed_at) VALUES (?,?,?,?,?,?)",
            (r["source"], r["source_id"], r["client_id"] or "", r["item_type"] or "",
             "", int(time.time())),
        )
        dst.execute(
            "INSERT INTO vec_items(rowid, embedding, client_id) VALUES (?,?,?)",
            (cur.lastrowid, struct.pack(f"{len(emb)}f", *emb), r["client_id"] or ""),
        )
        indexed += 1
        if (i + 1) % 500 == 0:
            dst.commit()
            rate = (i + 1) / (time.perf_counter() - t0)
            print(f"  {i + 1}/{total} ({rate:.0f}/s) indexed={indexed} failed={failed}", flush=True)
    dst.commit()
    dst.close()
    return {"total": total, "indexed": indexed, "failed": failed,
            "dim_mismatch": dim_mismatch, "elapsed_s": round(time.perf_counter() - t0, 1)}


def _smoke_recall(model: str, out: Path, n: int = 150, words: int = 14, offset: int = 8) -> dict:
    """Known-item recall sobre el DB nuevo: query = fragmento del doc, busca su vuelta."""
    con = _open_vec(out)
    rows = con.execute("SELECT rowid, source, source_id FROM vec_map ORDER BY rowid").fetchall()
    stride = max(1, len(rows) // n)
    sample = rows[::stride][:n]

    hits = {k: 0 for k in _KS}
    rr = 0.0
    evaluated = 0
    for r in sample:
        raw = sm._hydrate(r["source"], r["source_id"])
        text = raw[0] if isinstance(raw, tuple) and raw else raw
        if not isinstance(text, str) or not text.strip():
            continue
        text = sm._humanize_embedding_text(text)
        toks = text.split()
        query = " ".join(toks[offset:offset + words]) or " ".join(toks[:words])
        qe = _embed(model, query, "query")
        if not qe:
            continue
        found = con.execute(
            "SELECT rowid FROM vec_items WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (struct.pack(f"{len(qe)}f", *qe), max(_KS)),
        ).fetchall()
        ranked = [row["rowid"] for row in found]
        evaluated += 1
        if r["rowid"] in ranked:
            rank = ranked.index(r["rowid"]) + 1
            rr += 1.0 / rank
            for k in _KS:
                if rank <= k:
                    hits[k] += 1
    con.close()
    d = max(evaluated, 1)
    return {"evaluated": evaluated,
            "recall_at": {f"@{k}": round(hits[k] / d, 4) for k in _KS},
            "mrr": round(rr / d, 4)}


def main() -> int:
    """Reindexa y valida; nunca hace swap (eso es manual tras leer el recall)."""
    ap = argparse.ArgumentParser(description="Reindex no-destructivo del substrato vectorial")
    ap.add_argument("--model", required=True)
    ap.add_argument("--dim", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = ROOT / args.out
    stats = _reindex(args.model, args.dim, out)
    print(f"\n[reindex] listo: {json.dumps(stats)}", flush=True)
    if stats["indexed"] == 0:
        print("[FATAL] 0 indexados — no valido, no toques el índice vivo.", file=sys.stderr)
        return 1

    print("[smoke] known-item recall sobre el DB nuevo...", flush=True)
    rec = _smoke_recall(args.model, out)
    print(f"[smoke] {json.dumps(rec)}", flush=True)
    print(f"\n→ recall@5 nuevo índice ({args.model}) = {rec['recall_at']['@5']}  "
          f"| baseline bge-m3 (medido) = 0.925", flush=True)
    print("→ SWAP MANUAL solo si recall@5 >= ~0.90. Comandos en el docstring de este script.",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
