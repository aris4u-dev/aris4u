"""
V16 Novelty Detector — New-domain detection via claude-mem + atoms + exemplars.

Detects if a query is in a new domain (without prior atoms/memoria/exemplars context)
and activates deep exploration flag for the user's requirement: "dominios nuevos → profundidad mucho mayor".

Algorithm:
1. claude-mem FTS5 RECALL: query against observations (0 hits → signal)
2. knowledge_atoms match: embed query, cosine sim to atoms (max < 0.4 → signal)
3. exemplars distance: embed query, min distance to exemplars (min > 0.6 → signal)
4. Decision: is_new_domain = (2/3 signals True) with confidence weighting
5. Output: recommended_depth_override = "deep_exploration" if new

Performance (verified empirically 2026-04-24, post-F3-fix):
- claude-mem FTS5: ~3ms (320MB db, BM25 ranking, indexed)
- Embedding query (mxbai warm): ~25ms
- Similarity search with cached atoms+exemplars: ~5-10ms
- Total latency: ~40-60ms (previously ~4s before caching — F3 fix)

Caches: `_ATOMS_CACHE_PATH` (computed lazily, invalidated when knowledge_atoms.py changes)
and reuses `data/exemplar_embeddings.npz` produced by f1_classifier.

Note: claude-mem.db is READ-ONLY. Tests use in-memory SQLite + mocks.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from .config import OLLAMA_MAC_URL, SESSIONS_DB
from .exemplars import EXEMPLARS
from .knowledge_atoms import ATOMS

# Add tools directory to sys.path for _logger import (W2.1 requirement)
_TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

try:
    from _logger import emit_event
except ImportError:
    # Fallback: no-op emit_event if _logger unavailable (dev/test isolation)
    def emit_event(*args, **kwargs) -> None:
        pass


logger = logging.getLogger(__name__)
# CLAUDE_MEM_DB retirado en V18 Fase E (paso 10): novelty usa observations_local_fts (sessions.db).

# Disk caches for atom + exemplar embeddings (F3 fix — 2026-04-24).
# Without caching, _match_atoms + _exemplar_distance embedded ~200 strings
# per query (~4s total). With caching, only the query is embedded (~25ms).
_ATOMS_CACHE_PATH = Path(__file__).parent.parent.parent / "data" / "atoms_embeddings.npz"
_EXEMPLARS_CACHE_PATH = Path(__file__).parent.parent.parent / "data" / "exemplar_embeddings.npz"
_ATOMS_SOURCE = Path(__file__).parent / "knowledge_atoms.py"
_EXEMPLARS_SOURCE = Path(__file__).parent / "exemplars.py"

# Canary anti-cambio-de-modelo para la caché PROPIA de atoms (mismo patrón y valores que
# f1_classifier): si Ollama cambia el modelo de embeddings, los vectores cacheados quedan
# incompatibles → similitudes basura silenciosas. El gate de mtime NO lo detecta.
_CANARY_TEXT = "implementa el sistema de autenticacion con tokens JWT"
_CANARY_MIN_SIM = 0.95

# Module-level cached arrays (lazy-loaded on first call)
_atoms_embeddings: Optional[np.ndarray] = None
_exemplars_embeddings: Optional[np.ndarray] = None


def _atoms_cache_is_fresh(cached) -> bool:
    """¿La caché de atoms sigue siendo compatible con el modelo de embeddings actual?

    El gate de mtime no detecta que Ollama cambió de modelo (p.ej. mxbai re-normalizó L2)
    → vectores viejos incompatibles → cos≈0 silencioso (mismo modo-fallo que degradó el
    Depth Protocol). Re-embebe un canary y exige similitud alta con el valor cacheado.
    Throttled a 1 vez/6h (marker) para no pagar un embed extra por carga. Ollama caído →
    confía en la caché (no invalida). Espeja f1_classifier._cache_is_fresh.
    """
    if "_canary" not in cached:
        logger.info("Atoms cache sin canary (formato viejo) — recomputando una vez")
        return False
    marker = _ATOMS_CACHE_PATH.with_suffix(".canary_ok")
    try:
        if marker.exists() and (time.time() - marker.stat().st_mtime) < 6 * 3600:
            return True
    except Exception:
        pass
    live = _embed_text(_CANARY_TEXT)
    if live is None:
        return True  # Ollama caído: no invalidar una caché posiblemente buena
    sim = _cosine_similarity(np.asarray(live, dtype=np.float32), cached["_canary"])
    if sim < _CANARY_MIN_SIM:
        logger.warning(
            f"Atoms cache STALE (canary sim={sim:.2f} < {_CANARY_MIN_SIM}) — recomputando"
        )
        return False
    try:
        marker.touch()
    except Exception:
        pass
    return True


def _load_or_compute_atoms_cache() -> Optional[np.ndarray]:
    """Load atom embeddings from disk cache, or compute + save if missing/stale.

    Cache is invalidated when `knowledge_atoms.py` mtime > cache mtime.
    Returns a 2D array (n_atoms × embedding_dim) or None if embedding failed.
    """
    global _atoms_embeddings
    if _atoms_embeddings is not None:
        return _atoms_embeddings

    # Try disk cache first
    try:
        if _ATOMS_CACHE_PATH.exists():
            cache_mtime = _ATOMS_CACHE_PATH.stat().st_mtime
            source_mtime = _ATOMS_SOURCE.stat().st_mtime
            if cache_mtime > source_mtime:
                cached = np.load(_ATOMS_CACHE_PATH)
                if _atoms_cache_is_fresh(cached):
                    emb = cached["embeddings"]
                    _atoms_embeddings = emb
                    # Use local `emb` (non-Optional) so pyright can verify len() is safe
                    logger.debug(f"Loaded {len(emb)} atom embeddings from cache")
                    return _atoms_embeddings
    except Exception as e:
        logger.warning(f"Atoms cache load failed, recomputing: {e}")

    return _compute_and_cache_atoms()


def _compute_and_cache_atoms() -> Optional[np.ndarray]:
    """Embebe todos los atoms y persiste la caché npz junto a su canary.

    Si Ollama no responde al canary, NO persiste (evita envenenar el disco con ceros
    sin canary, que la próxima carga no podría detectar como stale).
    """
    global _atoms_embeddings
    all_atoms = [
        atom.get("content", "") for category_atoms in ATOMS.values() for atom in category_atoms
    ]
    if not all_atoms:
        return None

    embeddings = []
    for atom_content in all_atoms:
        emb = _embed_text(atom_content)
        if emb is None:
            logger.warning("Failed to embed atom (Ollama down?); caching zeros placeholder")
            embeddings.append(np.zeros(1024, dtype=np.float32))
        else:
            embeddings.append(emb)

    arr = np.array(embeddings, dtype=np.float32)
    canary = _embed_text(_CANARY_TEXT)
    if canary is None:
        logger.warning("Ollama no respondió al canary — no se persiste la caché de atoms")
        _atoms_embeddings = arr
        return arr
    try:
        _ATOMS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        np.savez(_ATOMS_CACHE_PATH, embeddings=arr, _canary=np.asarray(canary, dtype=np.float32))
        _ATOMS_CACHE_PATH.with_suffix(".canary_ok").touch()
        logger.info(f"Saved {len(arr)} atom embeddings to cache")
    except Exception as e:
        logger.warning(f"Atoms cache save failed (continuing in-memory): {e}")

    _atoms_embeddings = arr
    return arr


def _load_or_compute_exemplars_cache() -> Optional[np.ndarray]:
    """Load exemplar embeddings from disk cache (produced by f1_classifier).

    Reuses the same `data/exemplar_embeddings.npz` that f1_classifier maintains.
    Returns a flat array (n_exemplars × embedding_dim) combining all intent
    categories, or None if cache missing or stale.
    """
    global _exemplars_embeddings
    if _exemplars_embeddings is not None:
        return _exemplars_embeddings

    try:
        if not _EXEMPLARS_CACHE_PATH.exists():
            logger.warning("Exemplars cache missing; f1_classifier will build it on first use")
            return None

        cache_mtime = _EXEMPLARS_CACHE_PATH.stat().st_mtime
        source_mtime = _EXEMPLARS_SOURCE.stat().st_mtime
        if cache_mtime <= source_mtime:
            logger.warning(
                "Exemplars cache stale vs exemplars.py; skip until f1_classifier rebuilds it"
            )
            return None

        cached = np.load(_EXEMPLARS_CACHE_PATH, allow_pickle=True)
        # Flatten all intent categories into single array
        parts = []
        for intent in EXEMPLARS.keys():
            key = f"emb_{intent}"
            if key in cached:
                parts.append(np.array(cached[key]))
        if not parts:
            return None
        _exemplars_embeddings = np.vstack(parts).astype(np.float32)
        logger.debug(f"Loaded {len(_exemplars_embeddings)} exemplar embeddings from cache")
        return _exemplars_embeddings
    except Exception as e:
        logger.warning(f"Exemplars cache load failed: {e}")
        return None


@dataclass
class NoveltyResult:
    """Result of novelty detection.

    Attributes:
        is_new_domain: Whether the query is in a new domain (2/3 signals True)
        confidence: 0.0-1.0, weighted avg of 3 signals (higher = more novel)
        reasons: Evidence list explaining the decision
        recall_hits: Observations from claude-mem that matched query
        atoms_matched: Number of knowledge atoms with similarity > 0.4
        exemplars_distance: Min cosine distance to known exemplars (0.0-1.0)
        recommended_depth_override: "deep_exploration" if is_new_domain, else None
    """

    is_new_domain: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)
    recall_hits: int = 0
    atoms_matched: int = 0
    exemplars_distance: float = 1.0
    recommended_depth_override: Optional[str] = None


def _search_claude_mem(query: str, limit: int = 20) -> tuple[int, float]:
    """
    Search claude-mem.db via FTS5 for observations matching query.

    Args:
        query: Search query
        limit: Max results to return

    Returns:
        Tuple of (hit_count, max_bm25_score)
    """
    try:
        # V18 Fase E: FTS5 sobre el texto PROPIO (observations_local_fts en sessions.db),
        # no la claude-mem.db 3er-party muerta. Signal 1 del detector de novedad.
        if not SESSIONS_DB.exists():
            logger.warning(f"sessions.db not found at {SESSIONS_DB}")
            return 0, 0.0

        conn = sqlite3.connect(str(SESSIONS_DB), timeout=2.0)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # FTS5 search with BM25 ranking (built-in to SQLite FTS5).
        # Note: FTS5 MATCH clause cannot use placeholders, so escape query manually.
        safe_query = query.replace('"', '""')  # Escape quotes for FTS5
        cursor.execute(f"""
            SELECT COUNT(*) as cnt,
                   MAX(rank) as max_rank
            FROM observations_local_fts
            WHERE observations_local_fts MATCH '{safe_query}'
            LIMIT {limit}
            """)
        row = cursor.fetchone()
        conn.close()

        hit_count = row["cnt"] or 0
        max_rank = row["max_rank"] or 0.0

        # Convert BM25 rank (negative) to similarity (0-1)
        # BM25 rank is negative: -10.5 = high relevance
        max_score = max(0.0, min(1.0, 1.0 + (max_rank / 100.0))) if max_rank < 0 else 0.0

        logger.debug(
            f"claude-mem search '{query[:50]}' → {hit_count} hits, max_score={max_score:.3f}"
        )
        return hit_count, max_score

    except sqlite3.OperationalError as e:
        logger.warning(f"claude-mem search failed (db locked?): {e}")
        return 0, 0.0
    except Exception as e:
        logger.warning(f"claude-mem search error: {e}")
        return 0, 0.0


def _embed_text(text: str, model: str = "mxbai-embed-large") -> Optional[np.ndarray]:
    """
    Embed text via Ollama (reuse f1_classifier pattern).

    Args:
        text: Text to embed (truncated to 2000 chars)
        model: Embedding model name

    Returns:
        Embedding vector as np.ndarray, or None if embedding fails
    """
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                f"{OLLAMA_MAC_URL}/api/embeddings",
                "-d",
                json.dumps({"model": model, "prompt": text[:2000]}),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        data = json.loads(result.stdout)
        embedding = data.get("embedding")
        if embedding:
            return np.array(embedding, dtype=np.float32)
        return None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, FileNotFoundError):
        return None


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two vectors (0.0-1.0)."""
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    similarity = float(dot / (norm_a * norm_b))
    return max(0.0, min(1.0, similarity))


def _match_atoms(query_emb: np.ndarray) -> tuple[int, float]:
    """
    Match query embedding against all knowledge atoms.

    Uses disk-cached atom embeddings (F3 fix). Falls back to recomputing
    if cache is unavailable. Vectorized cosine similarity via numpy.

    Args:
        query_emb: Query embedding from _embed_text

    Returns:
        Tuple of (matched_count, max_similarity)
        matched_count = atoms with similarity > 0.4
    """
    cached = _load_or_compute_atoms_cache()
    if cached is None or len(cached) == 0:
        return 0, 0.0

    # Vectorized cosine similarity against all atoms in one numpy op
    query_norm = float(np.linalg.norm(query_emb))
    if query_norm == 0:
        return 0, 0.0
    atom_norms = np.linalg.norm(cached, axis=1)
    dots = cached @ query_emb
    # Avoid divide-by-zero on any zero-norm atom (embedding failed cache slot)
    with np.errstate(invalid="ignore", divide="ignore"):
        sims = np.where(atom_norms > 0, dots / (query_norm * atom_norms), 0.0)
    sims = np.clip(sims, 0.0, 1.0)

    matched = int((sims > 0.4).sum())
    max_sim = float(sims.max()) if len(sims) else 0.0

    logger.debug(f"Atoms: {matched} matched, max_sim={max_sim:.3f}")
    return matched, max_sim


def _exemplar_distance(query_emb: np.ndarray) -> float:
    """
    Compute min cosine distance to exemplar set.

    Uses disk-cached exemplar embeddings (shared with f1_classifier, F3 fix).
    Falls back to max distance (1.0) if cache unavailable — interpreted as
    "no near match" which is safe (activates exemplars_signal for novelty).

    Args:
        query_emb: Query embedding

    Returns:
        Min distance (0.0=same as exemplar, 1.0=orthogonal)
    """
    cached = _load_or_compute_exemplars_cache()
    if cached is None or len(cached) == 0:
        return 1.0

    query_norm = float(np.linalg.norm(query_emb))
    if query_norm == 0:
        return 1.0
    ex_norms = np.linalg.norm(cached, axis=1)
    dots = cached @ query_emb
    with np.errstate(invalid="ignore", divide="ignore"):
        sims = np.where(ex_norms > 0, dots / (query_norm * ex_norms), 0.0)
    sims = np.clip(sims, 0.0, 1.0)

    min_distance = float(1.0 - sims.max()) if len(sims) else 1.0
    logger.debug(f"Exemplars: min_distance={min_distance:.3f}")
    return min_distance


def detect_novelty(query: str) -> NoveltyResult:
    """
    Detect if a query is in a new domain.

    Algorithm:
    1. claude-mem FTS5 RECALL (0 hits OR max_score < 0.5 → signal)
    2. knowledge_atoms match (max_sim < 0.4 → signal)
    3. exemplars distance (min_distance > 0.6 → signal)
    4. Decision: is_new_domain = (2/3 signals True)
    5. Confidence = weighted avg of 3 signals

    Fail-safe policy: if embedding fails (Ollama down), the function assumes
    the query IS novel and recommends deep exploration. Rationale: this
    module exists specifically to escalate depth for novel domains; silently
    treating an unclassifiable query as familiar subverts the purpose.

    Args:
        query: User query string

    Returns:
        NoveltyResult with is_new_domain flag and recommendations
    """
    if not query or not query.strip():
        return NoveltyResult(
            is_new_domain=False,
            confidence=0.0,
            reasons=["Empty query"],
        )

    result = NoveltyResult(
        is_new_domain=False,
        confidence=0.0,
        reasons=[],
    )

    # Signal 1: claude-mem recall
    recall_hits, max_recall_score = _search_claude_mem(query)
    result.recall_hits = recall_hits
    recall_signal = recall_hits == 0 or max_recall_score < 0.5
    if recall_signal:
        result.reasons.append(f"0 observations in claude-mem (or max_score={max_recall_score:.2f})")

    # Signal 2: knowledge atoms
    query_emb = _embed_text(query)
    if query_emb is None:
        # Fail-safe: assume novel when we cannot verify familiarity.
        # This matches the module's purpose (escalate depth for unknown domains)
        # and avoids silently reverting to shallow depth on Ollama outages.
        logger.warning(
            "Failed to embed query — assuming novel domain (fail-safe for depth escalation)"
        )
        result.is_new_domain = True
        result.confidence = 0.5
        result.reasons.append("embedding failed — assumed novel for depth-escalation safety")
        result.recommended_depth_override = "deep_exploration"
        return result

    atoms_matched, max_atom_sim = _match_atoms(query_emb)
    result.atoms_matched = atoms_matched
    atoms_signal = max_atom_sim < 0.4
    if atoms_signal:
        result.reasons.append(f"0 atoms matched (max_sim={max_atom_sim:.2f} < 0.4)")

    # Signal 3: exemplars distance
    exemplars_distance = _exemplar_distance(query_emb)
    result.exemplars_distance = exemplars_distance
    exemplars_signal = exemplars_distance > 0.6
    if exemplars_signal:
        result.reasons.append(f"Exemplars distance {exemplars_distance:.2f} > 0.6 (no near match)")

    # Decision: 2/3 signals
    signals_active = sum([recall_signal, atoms_signal, exemplars_signal])
    result.is_new_domain = signals_active >= 2

    # Confidence: weighted average of 3 signals
    # Each signal contributes: signal_present ? 1.0 : 0.0
    # Weight: recall=0.4 (direct memory), atoms=0.3 (knowledge), exemplars=0.3 (known patterns)
    confidence = (
        (0.4 * float(recall_signal)) + (0.3 * float(atoms_signal)) + (0.3 * float(exemplars_signal))
    )
    result.confidence = confidence

    # Depth override
    if result.is_new_domain:
        result.recommended_depth_override = "deep_exploration"
    else:
        if not result.reasons:
            result.reasons.append("Domain has prior context (observations, atoms, or exemplars)")
        result.recommended_depth_override = None

    logger.info(
        f"Novelty detection: query='{query[:50]}' → "
        f"is_new={result.is_new_domain}, confidence={result.confidence:.2f}, "
        f"signals={signals_active}/3 ({recall_signal=}, {atoms_signal=}, {exemplars_signal=})"
    )

    # Emit structured event for V16.6 W2.1 logging
    emit_event(
        "novelty_check",
        "novelty_detector",
        query=query[:50],
        is_new_domain=result.is_new_domain,
        confidence=round(result.confidence, 2),
        signals_active=signals_active,
        recall_signal=recall_signal,
        atoms_signal=atoms_signal,
        exemplars_signal=exemplars_signal,
    )

    return result
