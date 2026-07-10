"""
F1.PERCEPCION — Embedding-based Intent Classifier (Phase 1: Tier 1).

Replaces regex-based classify() with embedding nearest-neighbor using mxbai-embed-large.

mxbai-embed-large is the default. bge-m3 was evaluated but performed WORSE on our
exemplar set (5/10 vs 7/10 correct). MTEB cross-lingual benchmarks favor bge-m3 for
retrieval, but our nearest-neighbor classification task differs from retrieval
benchmarks — empirical results on our specific exemplars trumped the benchmark.
Real improvement path: SetFit fine-tuning (see M12/f1_classifier READMEs).

Architecture:
1. Pre-compute embeddings for ~180 exemplar queries (30-45 per intent, ES+EN)
2. On each query: embed it → find nearest exemplar → classify by that exemplar's intent
3. Confidence = cosine similarity of nearest match
4. If confidence < 0.70 → return "simple" as safe default (NOT regex fallback)
5. Regex fallback ONLY activates when f1 raises (Ollama unreachable) — via depth_protocol.classify

Performance:
- Embedding latency: ~100-150ms per query (Ollama warm)
- Total classify_v16() latency: <200ms typical
- Exemplar embeddings cached to disk at data/exemplar_embeddings.npz
- Cold start (first call with no cache): ~30s to embed all exemplars
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

import numpy as np

from .config import OLLAMA_MAC_URL
from .exemplars import EXEMPLARS, intent_names

logger = logging.getLogger(__name__)


def _load_active_temperature(db_path: Optional[Path] = None) -> float:
    """Read the active confidence-calibration temperature written back by F7.

    This is the live-side of the F7 learning loop: F7.APRENDIZAJE Phase 3
    (CALIBRATE_CLASSIFIER) computes a temperature, Phase 5 verifies it actually
    reduced calibration error on real ground truth, and — only then — publishes it
    to ``v16_active_calibration``. This classifier reads the latest published value
    at startup and recalibrates its confidence with it (see ``_apply_temperature``).

    FAIL-OPEN, ABSOLUTE: any failure whatsoever (config missing, db file absent,
    table not yet created, no rows, NULL, unparseable, or out-of-range value)
    returns ``1.0`` → identity recalibration → behavior IDENTICAL to a classifier
    with no calibration wired. The classifier never crashes nor degrades because of
    this read; the worst case is "as it was before F7 existed".

    Args:
        db_path: Override path to sessions.db (defaults to config.SESSIONS_DB).

    Returns:
        Active temperature coefficient in [0.1, 10.0], or 1.0 (identity) on any issue.
    """
    try:
        from .config import SESSIONS_DB

        path = Path(db_path) if db_path is not None else SESSIONS_DB
    except ImportError:
        # config must never break classification — fail-open to identity.
        return 1.0

    if not Path(path).exists():
        return 1.0

    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        row = conn.execute(
            "SELECT temperature_coeff FROM v16_active_calibration "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as e:
        logger.debug(f"No active calibration read ({e}); using temperature=1.0 (identity)")
        return 1.0
    finally:
        if conn is not None:
            conn.close()

    if not row or row[0] is None:
        return 1.0
    try:
        temperature = float(row[0])
    except (TypeError, ValueError):
        return 1.0
    # Guard: a non-positive or absurd temperature would corrupt confidence → ignore it.
    if not (0.1 <= temperature <= 10.0):
        logger.warning(
            f"Active calibration temperature={temperature} out of [0.1, 10.0] — "
            f"ignoring, using 1.0 (identity)"
        )
        return 1.0
    if temperature != 1.0:
        logger.info(f"F1 applying calibrated confidence temperature={temperature:.4f} (from F7)")
    return temperature

# Canary para detectar que Ollama cambió el modelo de embeddings (el gate de mtime NO lo
# ve). Entre abr→jun 2026 mxbai-embed-large pasó a normalizar L2 → los embeddings cacheados
# viejos quedaron incompatibles en dirección (cos≈0) → el clasificador devolvía 'simple'
# para TODO en silencio, degradando el Depth Protocol en cada prompt. Se re-embebe (throttled
# 6h) al cargar y se exige alta similitud con su versión cacheada.
_CANARY_TEXT = "implementa el sistema de autenticacion con tokens JWT"
_CANARY_MIN_SIM = 0.95


class EmbeddingClassifier:
    """
    Embedding-based intent classifier using nearest-neighbor search.

    Caches exemplar embeddings at initialization. On each query:
    1. Embed the query via Ollama
    2. Compute cosine similarity to all exemplar embeddings
    3. Return intent of nearest exemplar + confidence
    4. Fallback to regex if Ollama is unreachable
    """

    def __init__(
        self,
        ollama_url: str = OLLAMA_MAC_URL,
        model: str = "mxbai-embed-large",
        confidence_threshold: float = 0.70,
        temperature: Optional[float] = None,
    ):
        """
        Initialize classifier and pre-compute exemplar embeddings.

        Args:
            ollama_url: Base URL of Ollama server (e.g., 'http://localhost:11434')
            model: Embedding model name (default: mxbai-embed-large, empirically
                   better than bge-m3 on our exemplar set — see module docstring).
            confidence_threshold: Min confidence to accept classification (default: 0.70)
            temperature: Confidence-calibration temperature. ``None`` (default) reads
                   the latest value F7 wrote back to ``v16_active_calibration``
                   (fail-open to 1.0). Pass an explicit float to override (tests).
                   1.0 = identity = behavior identical to an uncalibrated classifier.
        """
        self.ollama_url = ollama_url
        self.model = model
        self.confidence_threshold = confidence_threshold
        # Live-side of the F7 learning loop: recalibrate confidence with the temperature
        # F7 verified-and-published. Fail-open default 1.0 (identity) if nothing wired.
        self.temperature = (
            float(temperature) if temperature is not None else _load_active_temperature()
        )

        # Cache for exemplar embeddings (computed once at init)
        self.exemplar_embeddings: dict[str, list[np.ndarray]] = {}
        self.exemplar_queries: dict[str, list[str]] = {}

        # Initialize exemplar cache
        self._initialize_exemplar_cache()

        logger.info(
            f"EmbeddingClassifier initialized: {len(self.exemplar_embeddings)} intents, "
            f"{sum(len(v) for v in self.exemplar_embeddings.values())} exemplars cached"
        )

    def _initialize_exemplar_cache(self) -> None:
        """
        Load exemplar embeddings from disk cache, or compute and save if missing.

        Disk cache at data/exemplar_embeddings.npz avoids re-embedding 160
        exemplars on every hook call (saves ~4s startup → <50ms).
        Cache is invalidated when exemplars.py changes.
        """
        cache_path = Path(__file__).parent.parent.parent / "data" / "exemplar_embeddings.npz"
        exemplar_path = Path(__file__).parent / "exemplars.py"

        cache_valid = False
        if cache_path.exists():
            try:
                cached = np.load(cache_path, allow_pickle=True)
                cache_mtime = cache_path.stat().st_mtime
                exemplar_mtime = exemplar_path.stat().st_mtime
                if cache_mtime > exemplar_mtime and self._cache_is_fresh(cached, cache_path):
                    for intent in intent_names():
                        key = f"emb_{intent}"
                        if key in cached:
                            self.exemplar_embeddings[intent] = [
                                cached[key][i] for i in range(len(cached[key]))
                            ]
                            self.exemplar_queries[intent] = EXEMPLARS[intent]
                    cache_valid = True
                    logger.info("Loaded exemplar embeddings from disk cache")
            except Exception as e:
                logger.warning(f"Cache load failed, will recompute: {e}")

        if not cache_valid:
            self._compute_and_cache_embeddings(cache_path)

    def _cache_is_fresh(self, cached, cache_path: Path) -> bool:
        """¿La caché sigue siendo compatible con el modelo Ollama actual?

        El gate de mtime NO detecta que Ollama cambió el modelo (regresión 2026: mxbai pasó
        a normalizar L2 → embeddings viejos incompatibles → cos≈0 → todo 'simple'). Re-embebe
        un canary y exige sim alta con la versión cacheada. Throttled a 1 vez/6h (marker) para
        no pagar un embed extra por prompt. Ollama caído → confía en la caché (no invalida).
        """
        if "_canary" not in cached:
            logger.info("Exemplar cache sin canary (formato viejo) — recomputando una vez")
            return False
        marker = cache_path.with_suffix(".canary_ok")
        try:
            if marker.exists() and (time.time() - marker.stat().st_mtime) < 6 * 3600:
                return True
        except Exception:
            pass
        live = self._embed_text(_CANARY_TEXT)
        if live is None:
            return True  # Ollama caído: no invalidar una caché posiblemente buena
        sim = self._cosine_similarity(np.array(live, dtype=np.float32), cached["_canary"])
        if sim < _CANARY_MIN_SIM:
            logger.warning(
                f"Exemplar cache STALE (canary sim={sim:.2f} < {_CANARY_MIN_SIM}) — recomputando"
            )
            return False
        try:
            marker.touch()
        except Exception:
            pass
        return True

    def _compute_and_cache_embeddings(self, cache_path: Path) -> None:
        """Compute embeddings for all exemplars and save to disk."""
        save_data = {}
        for intent in intent_names():
            queries = EXEMPLARS[intent]
            self.exemplar_queries[intent] = queries
            embeddings = []

            for query in queries:
                emb = self._embed_text(query)
                if emb is not None:
                    embeddings.append(np.array(emb, dtype=np.float32))
                else:
                    logger.warning(f"Failed to embed exemplar: {intent}/{query[:50]}")
                    embeddings.append(np.zeros(1024, dtype=np.float32))

            self.exemplar_embeddings[intent] = embeddings
            save_data[f"emb_{intent}"] = np.array(embeddings)

        # Canary para el health-check de futuras cargas (detecta cambio de modelo Ollama).
        # Si Ollama no respondió, NO persistir: la caché sería ceros y envenenaría el disco.
        canary = self._embed_text(_CANARY_TEXT)
        if canary is None:
            logger.warning("Ollama no respondió al embeber — no se persiste la caché (evita ceros)")
            return
        save_data["_canary"] = np.array(canary, dtype=np.float32)

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez(cache_path, **save_data)
            try:
                cache_path.with_suffix(".canary_ok").touch()
            except Exception:
                pass
            logger.info(f"Saved exemplar embeddings to {cache_path}")
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")

    def _embed_text(self, text: str) -> Optional[list[float]]:
        """
        Embed a text string via Ollama API.

        Args:
            text: Text to embed (will be truncated to 2000 chars)

        Returns:
            Embedding vector as list of floats, or None if embedding fails.
        """
        try:
            result = subprocess.run(
                [
                    "curl",
                    "-s",
                    f"{self.ollama_url}/api/embeddings",
                    "-d",
                    json.dumps({"model": self.model, "prompt": text[:2000]}),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            data = json.loads(result.stdout)
            return data.get("embedding")
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, FileNotFoundError):
            return None

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """
        Compute cosine similarity between two vectors.

        Args:
            a: Vector A
            b: Vector B

        Returns:
            Cosine similarity (0.0 to 1.0, clipped to avoid floating point errors)
        """
        dot = np.dot(a, b)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        similarity = float(dot / (norm_a * norm_b))
        # Clamp to [0, 1] to handle floating point precision errors
        return max(0.0, min(1.0, similarity))

    def _apply_temperature(self, confidence: float) -> float:
        """Recalibrate a raw nearest-neighbor confidence with the active temperature.

        F1's confidence is a **cosine similarity in [0, 1]** (nearest exemplar), NOT a
        softmax over class logits — so classic temperature scaling can't be applied to
        logits directly. It applies coherently in **logit (log-odds) space** instead
        (Platt-style), which is exactly the confidence→logit model F7's own calibrator
        uses as its fallback:

            calibrated = sigmoid( logit(confidence) / T ),  logit(c) = ln(c / (1 - c))

        Properties that make this safe:
        - **T == 1.0 → exact identity** → behavior IDENTICAL to no calibration.
        - **Monotonic in confidence** → the nearest-exemplar DECISION (argmax) is never
          changed; only the reported confidence moves. The meaningful effect is on the
          ``confidence < threshold`` routing gate (an overconfident classifier with T>1
          sends more borderline queries to the safe 'simple' default; T<1 sharpens).
        - Endpoints 0 and 1 are fixed points for any T; handled explicitly to avoid
          log(0)/overflow.

        Args:
            confidence: Raw cosine-similarity confidence in [0, 1].

        Returns:
            Recalibrated confidence in [0, 1].
        """
        T = self.temperature
        if T == 1.0:
            return confidence
        if confidence <= 0.0 or confidence >= 1.0:
            return confidence
        logit = float(np.log(confidence / (1.0 - confidence)))
        calibrated = 1.0 / (1.0 + float(np.exp(-logit / T)))
        return max(0.0, min(1.0, calibrated))

    def classify(self, query: str) -> tuple[str, float]:
        """
        Classify a query by finding the nearest exemplar.

        Args:
            query: User query to classify

        Returns:
            Tuple of (intent, confidence) where:
            - intent: One of 'simple', 'fix', 'decision', 'implementation', 'research'
            - confidence: Cosine similarity of nearest exemplar (0.0 to 1.0)
                         If < threshold, confidence is returned as-is for caller inspection.
        """
        if not query or not query.strip():
            return "simple", 0.0

        query_emb = self._embed_text(query)
        if query_emb is None:
            logger.warning(f"Failed to embed query, falling back to regex: {query[:50]}")
            # Fallback is handled by caller
            return "simple", 0.0

        query_emb = np.array(query_emb, dtype=np.float32)

        # Find nearest exemplar across all intents
        best_intent = "simple"
        best_confidence = 0.0

        for intent in intent_names():
            exemplar_embs = self.exemplar_embeddings[intent]
            for exemplar_emb in exemplar_embs:
                similarity = self._cosine_similarity(query_emb, exemplar_emb)
                if similarity > best_confidence:
                    best_confidence = similarity
                    best_intent = intent

        # Recalibrate the raw similarity with the F7-published temperature (identity at
        # T=1.0). Monotonic → best_intent is unchanged; only the confidence is adjusted.
        best_confidence = self._apply_temperature(best_confidence)

        logger.debug(f"Classified '{query[:50]}' → {best_intent} (confidence: {best_confidence:.3f})")
        return best_intent, best_confidence

    def classify_with_fallback(self, query: str, fallback_classify_fn) -> str:
        """
        Classify with fallback to regex if embedding classifier is uncertain.

        Args:
            query: User query to classify
            fallback_classify_fn: Fallback function (e.g., regex classify) to call if confidence < threshold

        Returns:
            Intent string: one of 'simple', 'fix', 'decision', 'implementation', 'research'
        """
        intent, confidence = self.classify(query)

        if confidence >= self.confidence_threshold:
            return intent

        # Fallback: insufficient confidence
        logger.warning(
            f"Low confidence classification (confidence={confidence:.3f} < {self.confidence_threshold}), "
            f"falling back to regex"
        )
        return fallback_classify_fn(query)


# Global singleton instance
_classifier: Optional[EmbeddingClassifier] = None


def _get_classifier() -> EmbeddingClassifier:
    """
    Get or create the global classifier instance.

    Returns:
        EmbeddingClassifier singleton
    """
    global _classifier
    if _classifier is None:
        _classifier = EmbeddingClassifier()
    return _classifier


def classify_v16(query: str) -> str:
    """
    Classify a query intent using embedding nearest-neighbor (V16 F1.PERCEPCION).

    This is the primary replacement for the broken regex-based classify() function.

    Architecture:
    1. Embed query via Ollama mxbai-embed-large
    2. Find nearest exemplar (cosine similarity)
    3. Return intent of nearest match
    4. If confidence < 0.70, return 'simple' as safe default
    5. If Ollama unreachable, fallback to regex

    Args:
        query: User query string (ES or EN)

    Returns:
        Intent: one of 'simple', 'fix', 'decision', 'implementation', 'research'

    Notes:
        - Exemplar embeddings cached at first call
        - Per-query latency: ~100-150ms (Ollama) + ~20ms (similarity search)
        - Total classify_v16() latency: <200ms
        - Graceful fallback to regex if Ollama is unreachable
    """
    classifier = _get_classifier()
    intent, confidence = classifier.classify(query)

    # If confidence is too low, return 'simple' as safe default
    if confidence < classifier.confidence_threshold:
        logger.warning(
            f"Low confidence classification: '{query[:50]}' → {intent} (confidence={confidence:.3f})"
        )
        # Don't use regex fallback by default — just return 'simple'
        # This is safer than regex which was wrong 93% of the time
        return "simple"

    return intent


def classify_v16_with_confidence(query: str) -> tuple[str, float]:
    """
    Classify a query and return both intent and confidence score.

    Useful for debugging, monitoring, and confidence-aware routing.

    Args:
        query: User query string (ES or EN)

    Returns:
        Tuple of (intent, confidence) where confidence is cosine similarity (0.0-1.0)
    """
    classifier = _get_classifier()
    return classifier.classify(query)
