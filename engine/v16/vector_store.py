"""ARIS4U WS3 — sidecar sqlite-vec vector store (data/aris_vectors.db).

ARIS4U-owned semantic-recall index. Embeddings come from the local Mac Ollama
via session_manager.embed_text (EMBED_MODEL = bge-m3, 1024 dims) — PHI-safe, no external
API call. NOTE: el clasificador de intención F1 usa OTRO embedder a propósito
(mxbai-embed-large; bge-m3 dio peor en su exemplar set 5/10 vs 7/10 — ver f1_classifier.py).
NO unificar los dos: son embedders distintos por decisión empírica. The index points at
content living in claude-mem.db (observations) and sessions.db (decisions) by
(source, source_id), so those databases stay immutable and the external claude-mem tool
can never clobber ARIS4U's vectors. KNN search supports per-client isolation via a vec0
metadata column (sentinel "" for unscoped, since vec0 rejects NULL on TEXT columns).

Graceful degradation: if the sqlite-vec extension cannot load, available() returns False
and every public call becomes a no-op — callers keep working on FTS5 + brute-force cosine.
"""

import hashlib
import sqlite3
import struct
import threading
import time
from typing import Optional

from .config import (
    ARIS_VECTORS_DB,
    BUSY_TIMEOUT_MS,
    EMBED_DIM,
    NO_CLIENT_SENTINEL,
    VECTOR_DEFAULT_K,
)

_write_lock = threading.Lock()
_available: Optional[bool] = None


def available() -> bool:
    """Return True if sqlite-vec can be loaded in this interpreter (cached)."""
    global _available
    if _available is None:
        try:
            import sqlite_vec  # type: ignore[import-not-found]  # noqa: F401

            con = sqlite3.connect(":memory:")
            con.enable_load_extension(True)
            sqlite_vec.load(con)
            con.execute("SELECT vec_version()").fetchone()
            con.close()
            _available = True
        except Exception:
            _available = False
    return _available


def _connect() -> sqlite3.Connection:
    """Open the sidecar DB with sqlite-vec loaded. Caller must close()."""
    import sqlite_vec  # type: ignore[import-not-found]

    ARIS_VECTORS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(ARIS_VECTORS_DB), timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return con


def init_store() -> bool:
    """Create the vec0 index + side map if missing. Idempotent. No-op if unavailable."""
    if not available():
        return False
    with _write_lock:
        con = _connect()
        try:
            con.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_items USING vec0("
                f"embedding float[{EMBED_DIM}] distance_metric=cosine, client_id text)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS vec_map ("
                "rowid INTEGER PRIMARY KEY AUTOINCREMENT, "
                "source TEXT NOT NULL, source_id TEXT NOT NULL, "
                "client_id TEXT NOT NULL DEFAULT '', item_type TEXT DEFAULT '', "
                "content_hash TEXT, indexed_at INTEGER, "
                "UNIQUE(source, source_id))"
            )
            con.commit()
        finally:
            con.close()
    return True


def _embed(text: str, role: str = "doc") -> Optional[list[float]]:
    """Embed via the existing Mac-local embedder (lazy import avoids an import cycle).

    role='doc' indexa, role='query' consulta — asimétrico para EmbeddingGemma/arctic.
    """
    from .session_manager import embed_text

    return embed_text(text, role=role)


def _pack(emb: list[float]) -> bytes:
    return struct.pack(f"{len(emb)}f", *emb)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def _upsert(source: str, source_id: str, embedding: list[float],
            client_id: str = "", item_type: str = "", content_hash: str = "") -> str:
    """Insert/replace one vector keyed by (source, source_id). Embedding precomputed.

    Returns "indexed", "updated", "skipped", or "failed". Used directly by tests (no Ollama call).
    """
    # A0.8: excluir provenance/git-commit del índice — ocupan slots del top-K sin valor semántico.
    if item_type in ("provenance", "git-commit"):
        return "skipped"
    if not available() or not embedding or len(embedding) != EMBED_DIM:
        return "failed"
    client_id = client_id or NO_CLIENT_SENTINEL
    blob = _pack(embedding)
    with _write_lock:
        con = _connect()
        try:
            existing = con.execute(
                "SELECT rowid, content_hash FROM vec_map WHERE source=? AND source_id=?",
                (source, source_id),
            ).fetchone()
            now = int(time.time())
            if existing is None:
                cur = con.execute(
                    "INSERT INTO vec_map(source, source_id, client_id, item_type, "
                    "content_hash, indexed_at) VALUES (?,?,?,?,?,?)",
                    (source, source_id, client_id, item_type, content_hash, now),
                )
                rowid = cur.lastrowid
                con.execute(
                    "INSERT INTO vec_items(rowid, embedding, client_id) VALUES (?,?,?)",
                    (rowid, blob, client_id),
                )
                con.commit()
                return "indexed"
            else:
                rowid = existing["rowid"]
                con.execute("DELETE FROM vec_items WHERE rowid=?", (rowid,))
                con.execute(
                    "INSERT INTO vec_items(rowid, embedding, client_id) VALUES (?,?,?)",
                    (rowid, blob, client_id),
                )
                con.execute(
                    "UPDATE vec_map SET client_id=?, item_type=?, content_hash=?, "
                    "indexed_at=? WHERE rowid=?",
                    (client_id, item_type, content_hash, now, rowid),
                )
                con.commit()
                return "updated"
        finally:
            con.close()


def index_item(source: str, source_id: str, text: str,
               client_id: str = "", item_type: str = "") -> str:
    """Embed + index one item. Idempotent: skips if content unchanged.

    Returns "skipped", "indexed", "updated", "failed", or "unavailable".
    """
    if not available():
        return "unavailable"
    if not text or not text.strip():
        return "failed"
    content_hash = _hash(text)
    con = _connect()
    try:
        existing = con.execute(
            "SELECT content_hash FROM vec_map WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
    finally:
        con.close()
    if existing is not None and existing["content_hash"] == content_hash:
        return "skipped"
    emb = _embed(text)
    if not emb:
        return "failed"
    return _upsert(source, source_id, emb, client_id, item_type, content_hash)


def search(query: str, client_id: Optional[str] = None,
           k: int = VECTOR_DEFAULT_K) -> list[dict]:
    """KNN semantic search. Filters by client_id when given (per-client isolation).

    Returns [{source, source_id, client_id, item_type, distance, similarity}], or [] if
    unavailable / empty / embed failure. Never raises (degrades to []).
    """
    if not available() or not query or not query.strip():
        return []
    try:
        qemb = _embed(query, role="query")
        if not qemb or len(qemb) != EMBED_DIM:
            return []
        con = _connect()
        try:
            # Soft-scoping (FREEZE fix de validez): con cliente, traer SUS vectores +
            # los SIN DUEÑO (sentinel ""), NUNCA los de otro cliente — el invariante
            # cross-client (A7) se preserva idéntico. El filtro de metadata se aplica
            # POST-KNN en vec0, así que se sobre-pide (over-fetch) y se trunca a k al
            # final; sin esto los vectores del cliente pueden no entrar al top-k GLOBAL
            # y el recall scoped devuelve 0 aunque existan (causa raíz del bug medido:
            # 84% de recalls vacíos por n_semantic==0 en proyectos de cliente).
            fetch_k = k if client_id is None else min(k * 3, k + 50)
            params: list = [_pack(qemb), fetch_k]
            where = "WHERE v.embedding MATCH ? AND k = ?"
            if client_id is not None:
                where += " AND v.client_id IN (?, ?)"
                params += [client_id or NO_CLIENT_SENTINEL, NO_CLIENT_SENTINEL]
            rows = con.execute(
                f"SELECT v.rowid AS rowid, v.distance AS distance, "
                f"m.source AS source, m.source_id AS source_id, "
                f"m.client_id AS client_id, m.item_type AS item_type "
                f"FROM vec_items v JOIN vec_map m ON m.rowid = v.rowid "
                f"{where} ORDER BY v.distance",
                params,
            ).fetchall()
        finally:
            con.close()
        out = []
        for r in rows:
            d = float(r["distance"])
            out.append({
                "source": r["source"],
                "source_id": r["source_id"],
                "client_id": r["client_id"],
                "item_type": r["item_type"],
                "distance": round(d, 4),
                "similarity": round(1.0 - d, 4),  # cosine distance -> similarity
            })
        return out[:k]  # truncar tras el post-filtro de metadata (over-fetch arriba)
    except Exception:
        return []


def delete_item(source: str, source_id: str) -> bool:
    """Remove one item from the index. Returns True if a row was deleted."""
    if not available():
        return False
    with _write_lock:
        con = _connect()
        try:
            row = con.execute(
                "SELECT rowid FROM vec_map WHERE source=? AND source_id=?",
                (source, source_id),
            ).fetchone()
            if row is None:
                return False
            con.execute("DELETE FROM vec_items WHERE rowid=?", (row["rowid"],))
            con.execute("DELETE FROM vec_map WHERE rowid=?", (row["rowid"],))
            con.commit()
            return True
        finally:
            con.close()


def get_stats() -> dict:
    """Return index counts overall and per source. Safe if unavailable/empty."""
    if not available():
        return {"available": False, "total": 0, "by_source": {}}
    con = _connect()
    try:
        try:
            total = con.execute("SELECT COUNT(*) FROM vec_map").fetchone()[0]
            by_source = {
                r["source"]: r["n"]
                for r in con.execute(
                    "SELECT source, COUNT(*) AS n FROM vec_map GROUP BY source"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            return {"available": True, "total": 0, "by_source": {}}
        return {"available": True, "total": total, "by_source": by_source}
    finally:
        con.close()
