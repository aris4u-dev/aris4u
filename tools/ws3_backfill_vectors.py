#!/usr/bin/env python3
"""WS3 backfill — index observations_local + decisions (sessions.db) into the
ARIS4U sqlite-vec sidecar (data/aris_vectors.db). V18 Fase E: texto propio, no claude-mem.

Idempotent & resumable: items whose content hash already matches in the sidecar are
skipped without re-embedding. Embeddings via local Mac Ollama (mxbai-embed-large, 1024d)
through the batch /api/embed endpoint, falling back to per-item /api/embeddings.
PHI-safe: reads live DBs read-only, writes only the sidecar, never calls an external API.

Usage:
    .venv312/bin/python tools/ws3_backfill_vectors.py --source all
    .venv312/bin/python tools/ws3_backfill_vectors.py --source observations --limit 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root importable

from engine.v16 import vector_store as vs  # noqa: E402
from engine.v16.config import (  # noqa: E402
    EMBED_DIM,
    EMBED_MODEL,
    OLLAMA_MAC_URL,
    SESSIONS_DB,
)

Item = tuple[str, str, str, str, str]  # (source, source_id, text, client_id, item_type)


def _ro(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def embed_batch(texts: list[str]) -> list[list[float] | None]:
    """Embed a batch via /api/embed; fall back to per-item /api/embeddings on failure."""
    try:
        payload = {"model": EMBED_MODEL, "input": [t[:4000] for t in texts]}
        r = subprocess.run(
            ["curl", "-s", f"{OLLAMA_MAC_URL}/api/embed", "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=120,
        )
        embs = json.loads(r.stdout).get("embeddings")
        if embs and len(embs) == len(texts) and all(len(e) == EMBED_DIM for e in embs):
            return embs
    except Exception:
        pass
    out: list[list[float] | None] = []
    for t in texts:
        try:
            r = subprocess.run(
                ["curl", "-s", f"{OLLAMA_MAC_URL}/api/embeddings",
                 "-d", json.dumps({"model": EMBED_MODEL, "prompt": t[:4000]})],
                capture_output=True, text=True, timeout=20,
            )
            e = json.loads(r.stdout).get("embedding")
            out.append(e if e and len(e) == EMBED_DIM else None)
        except Exception:
            out.append(None)
    return out


def collect_observations(limit: int | None = None) -> list[Item]:
    # V18 Fase E: indexa el texto PROPIO (observations_local en sessions.db), no la
    # claude-mem.db 3er-party archivada. Cada fila tiene `content` (COALESCE ya aplicado
    # en la migración) → nuevas observaciones del mirror se vectorizan al sidecar.
    con = _ro(SESSIONS_DB)
    q = ("SELECT id, content, type, client_id FROM observations_local "
         "WHERE COALESCE(content,'') <> ''")
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = con.execute(q).fetchall()
    con.close()
    out: list[Item] = []
    for r in rows:
        content = str(r["content"] or "").strip()
        if content:
            out.append(("observations", str(r["id"]), content, r["client_id"] or "",
                        r["type"] or ""))
    return out


def collect_decisions(limit: int | None = None) -> list[Item]:
    con = _ro(SESSIONS_DB)
    q = ("SELECT id, decision, rationale, domain, client_id FROM decisions "
         "WHERE decision IS NOT NULL AND trim(decision) <> ''")
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = con.execute(q).fetchall()
    con.close()
    out: list[Item] = []
    for r in rows:
        text = r["decision"] or ""
        if r["rationale"]:
            text += " — " + r["rationale"]
        out.append(("decisions", str(r["id"]), text, r["client_id"] or "",
                    r["domain"] or ""))
    return out


def _existing_hashes() -> dict[tuple[str, str], str]:
    con = vs._connect()
    try:
        return {(r["source"], r["source_id"]): r["content_hash"]
                for r in con.execute(
                    "SELECT source, source_id, content_hash FROM vec_map").fetchall()}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["observations", "decisions", "all"], default="all")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

    if not vs.available():
        print("FATAL: sqlite-vec unavailable in this interpreter")
        sys.exit(1)
    vs.init_store()

    items: list[Item] = []
    if args.source in ("observations", "all"):
        items += collect_observations(args.limit)
    if args.source in ("decisions", "all"):
        items += collect_decisions(args.limit)
    print(f"[backfill] candidates: {len(items)} (source={args.source})", flush=True)

    existing = _existing_hashes()
    todo = []
    skipped = 0
    for source, sid, text, client, itype in items:
        h = _hash(text)
        if existing.get((source, sid)) == h:
            skipped += 1
            continue
        todo.append((source, sid, text, client, itype, h))
    print(f"[backfill] to embed: {len(todo)} | already-current: {skipped}", flush=True)

    indexed = failed = 0
    t0 = time.time()
    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        embs = embed_batch([c[2] for c in chunk])
        for (source, sid, text, client, itype, h), emb in zip(chunk, embs):
            if not emb:
                failed += 1
                continue
            res = vs._upsert(source, sid, emb, client, itype, h)
            if res in ("indexed", "updated"):
                indexed += 1
            else:
                failed += 1
        done = i + len(chunk)
        el = time.time() - t0
        rate = done / el if el else 0
        print(f"[backfill] {done}/{len(todo)} indexed={indexed} failed={failed} "
              f"{rate:.0f}/s", flush=True)

    el = time.time() - t0
    print(f"[backfill] DONE indexed={indexed} skipped={skipped} failed={failed} "
          f"in {el:.0f}s", flush=True)
    print(f"[backfill] stats: {vs.get_stats()}", flush=True)


if __name__ == "__main__":
    main()
