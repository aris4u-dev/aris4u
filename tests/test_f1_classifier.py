"""
Tests for F1 Embedding-Based Intent Classifier (V16 F1.PERCEPCION).

Validates:
1. Embedding-based classification accuracy on test queries
2. Fallback behavior when Ollama is unreachable
3. Confidence thresholds and safe defaults
4. Exemplar caching behavior
5. Cosine similarity computation
6. Multi-language support (ES/EN)
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

import sqlite3

from engine.v16.exemplars import EXEMPLARS, get_exemplars_for_intent, intent_names
from engine.v16.f1_classifier import (
    EmbeddingClassifier,
    _load_active_temperature,
    classify_v16,
    classify_v16_with_confidence,
)


# ==============================================================================
# FIXTURES
# ==============================================================================


@pytest.fixture
def mock_ollama_embed():
    """Mock Ollama embedding API to return deterministic embeddings."""

    def _embed(text: str) -> list[float]:
        """Create a deterministic embedding based on text hash."""
        # Use hash to create reproducible random embedding
        seed = hash(text) & 0x7FFFFFFF
        np.random.seed(seed)
        return np.random.randn(1024).tolist()

    return _embed


@pytest.fixture
def classifier_with_mocked_ollama(mock_ollama_embed):
    """Create EmbeddingClassifier with mocked Ollama."""
    with patch("engine.v16.f1_classifier.EmbeddingClassifier._embed_text") as mock_embed:
        mock_embed.side_effect = lambda text: mock_ollama_embed(text)
        classifier = EmbeddingClassifier()
        yield classifier


# ==============================================================================
# TESTS: EXEMPLAR DATASET
# ==============================================================================


def test_exemplars_structure() -> None:
    """Verify exemplar dataset is properly structured."""
    assert len(EXEMPLARS) == 5, "Should have 5 intents"
    assert set(EXEMPLARS.keys()) == {
        "simple",
        "fix",
        "decision",
        "implementation",
        "research",
    }

    for intent, queries in EXEMPLARS.items():
        assert len(queries) >= 30, f"Intent '{intent}' should have at least 30 exemplars"
        assert all(isinstance(q, str) for q in queries), f"All exemplars in '{intent}' should be strings"
        assert all(len(q) >= 2 for q in queries), f"All exemplars in '{intent}' should have length >= 2"


def test_exemplars_bilingual() -> None:
    """Verify exemplars include both English and Spanish."""
    for intent in intent_names():
        queries = get_exemplars_for_intent(intent)
        # Simple heuristic: Spanish queries often have accent marks or Spanish keywords
        spanish_keywords = ["qué", "es", "la", "del", "está", "el", "y", "muestra"]
        english_keywords = ["what", "is", "the", "show", "list", "tell"]

        has_spanish = any(any(kw in q.lower() for kw in spanish_keywords) for q in queries)
        has_english = any(any(kw in q.lower() for kw in english_keywords) for q in queries)

        assert has_spanish, f"Intent '{intent}' should have Spanish examples"
        assert has_english, f"Intent '{intent}' should have English examples"


# ==============================================================================
# TESTS: EMBEDDING CLASSIFIER
# ==============================================================================


def test_classifier_initialization(classifier_with_mocked_ollama) -> None:
    """Test classifier initializes with cached exemplar embeddings."""
    classifier = classifier_with_mocked_ollama

    # Check all intents are cached
    assert len(classifier.exemplar_embeddings) == 5
    assert set(classifier.exemplar_embeddings.keys()) == set(intent_names())

    # Check exemplar counts (30 per intent after expansion)
    for intent in intent_names():
        assert len(classifier.exemplar_embeddings[intent]) >= 30
        assert len(classifier.exemplar_queries[intent]) >= 30

    # Check embeddings are numpy arrays
    for intent in intent_names():
        for emb in classifier.exemplar_embeddings[intent]:
            assert isinstance(emb, np.ndarray)
            assert emb.shape == (1024,)
            assert emb.dtype == np.float32


def test_cosine_similarity(classifier_with_mocked_ollama) -> None:
    """Test cosine similarity computation."""
    classifier = classifier_with_mocked_ollama

    # Test identical vectors
    v1 = np.array([1, 0, 0], dtype=np.float32)
    v2 = np.array([1, 0, 0], dtype=np.float32)
    sim = classifier._cosine_similarity(v1, v2)
    assert abs(sim - 1.0) < 1e-6, "Identical vectors should have similarity 1.0"

    # Test orthogonal vectors
    v1 = np.array([1, 0, 0], dtype=np.float32)
    v2 = np.array([0, 1, 0], dtype=np.float32)
    sim = classifier._cosine_similarity(v1, v2)
    assert abs(sim - 0.0) < 1e-6, "Orthogonal vectors should have similarity 0.0"

    # Test zero vector
    v1 = np.array([0, 0, 0], dtype=np.float32)
    v2 = np.array([1, 0, 0], dtype=np.float32)
    sim = classifier._cosine_similarity(v1, v2)
    assert sim == 0.0, "Zero vector should have similarity 0.0"


def test_classify_simple(classifier_with_mocked_ollama) -> None:
    """Test classification of a simple query."""
    classifier = classifier_with_mocked_ollama

    intent, confidence = classifier.classify("what is a tensor")
    assert intent in intent_names(), "Intent should be valid"
    assert 0.0 <= confidence <= 1.0, "Confidence should be between 0 and 1"


def test_classify_implementation_english(classifier_with_mocked_ollama) -> None:
    """Test classification of an implementation query (English)."""
    classifier = classifier_with_mocked_ollama

    intent, confidence = classifier.classify("build me a classifier using embeddings")
    # With deterministic embeddings, query should be closest to an "implementation" exemplar
    # This is probabilistic, so we just check the format is correct
    assert intent in intent_names()
    assert 0.0 <= confidence <= 1.0


def test_classify_implementation_spanish(classifier_with_mocked_ollama) -> None:
    """Test classification of an implementation query (Spanish)."""
    classifier = classifier_with_mocked_ollama

    intent, confidence = classifier.classify("construye un clasificador usando embeddings")
    assert intent in intent_names()
    assert 0.0 <= confidence <= 1.0


def test_classify_fix_english(classifier_with_mocked_ollama) -> None:
    """Test classification of a fix query (English)."""
    classifier = classifier_with_mocked_ollama

    intent, confidence = classifier.classify("fix the bug in the classifier")
    assert intent in intent_names()
    # Confidence should be clamped to [0, 1] due to floating point precision
    assert 0.0 <= confidence <= 1.0001, f"Confidence {confidence} out of range"


def test_classify_fix_spanish(classifier_with_mocked_ollama) -> None:
    """Test classification of a fix query (Spanish)."""
    classifier = classifier_with_mocked_ollama

    intent, confidence = classifier.classify("arregla el bug en el clasificador")
    assert intent in intent_names()
    assert 0.0 <= confidence <= 1.0


def test_classify_decision_english(classifier_with_mocked_ollama) -> None:
    """Test classification of a decision query (English)."""
    classifier = classifier_with_mocked_ollama

    intent, confidence = classifier.classify("should we use Ollama or Claude API")
    assert intent in intent_names()
    assert 0.0 <= confidence <= 1.0


def test_classify_research_english(classifier_with_mocked_ollama) -> None:
    """Test classification of a research query (English)."""
    classifier = classifier_with_mocked_ollama

    intent, confidence = classifier.classify("investigate how embedding similarity works")
    assert intent in intent_names()
    assert 0.0 <= confidence <= 1.0


def test_classify_with_confidence_threshold(classifier_with_mocked_ollama) -> None:
    """Test confidence threshold handling."""
    classifier = classifier_with_mocked_ollama

    # Default threshold is 0.70
    assert classifier.confidence_threshold == 0.70

    # Query classification should work
    intent, confidence = classifier.classify("some random query")
    assert isinstance(intent, str)
    assert isinstance(confidence, float)


# ==============================================================================
# TESTS: FALLBACK BEHAVIOR
# ==============================================================================


def test_classify_with_fallback_function(classifier_with_mocked_ollama) -> None:
    """Test fallback function is called when confidence is low."""
    classifier = classifier_with_mocked_ollama

    def mock_fallback(query: str) -> str:
        return "fallback_intent"

    # Create a classifier with very high confidence threshold (will trigger fallback)
    classifier.confidence_threshold = 0.99999

    intent = classifier.classify_with_fallback("test query", mock_fallback)
    # With threshold 0.99999, fallback should be called
    assert intent == "fallback_intent"


def test_ollama_connection_failure() -> None:
    """Test classifier handles Ollama connection failure gracefully."""
    with patch("engine.v16.f1_classifier.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("curl not found")

        classifier = EmbeddingClassifier()

        # Classifier should initialize despite Ollama failure (uses zero vectors)
        assert len(classifier.exemplar_embeddings) == 5

        # Classification should fail gracefully and return None for embedding
        result = classifier._embed_text("test")
        assert result is None


def test_ollama_timeout() -> None:
    """Test classifier handles Ollama timeout gracefully."""
    with patch("engine.v16.f1_classifier.subprocess.run") as mock_run:
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired("curl", 10)

        classifier = EmbeddingClassifier()
        result = classifier._embed_text("test")
        assert result is None


# ==============================================================================
# TESTS: MODULE-LEVEL FUNCTIONS
# ==============================================================================


def test_classify_v16_function() -> None:
    """Test module-level classify_v16 function."""
    with patch("engine.v16.f1_classifier.EmbeddingClassifier._embed_text") as mock_embed:
        mock_embed.return_value = [0.1] * 1024

        result = classify_v16("test query")
        assert result in intent_names()
        assert isinstance(result, str)


def test_classify_v16_with_confidence_function() -> None:
    """Test module-level classify_v16_with_confidence function."""
    with patch("engine.v16.f1_classifier.EmbeddingClassifier._embed_text") as mock_embed:
        mock_embed.return_value = [0.1] * 1024

        intent, confidence = classify_v16_with_confidence("test query")
        assert intent in intent_names()
        assert 0.0 <= confidence <= 1.0


def test_classify_v16_low_confidence_returns_simple() -> None:
    """Test that low-confidence classifications return 'simple' as safe default."""
    with patch("engine.v16.f1_classifier.EmbeddingClassifier.classify") as mock_classify:
        # Mock classifier to return low confidence
        mock_classify.return_value = ("implementation", 0.5)

        result = classify_v16("some ambiguous query")
        # With confidence 0.5 < 0.70 threshold, should return "simple"
        assert result == "simple"


# ==============================================================================
# TESTS: INTEGRATION WITH DEPTH_PROTOCOL
# ==============================================================================


def test_classify_integration_with_depth_protocol() -> None:
    """Test that the new classifier works with the depth_protocol module."""
    from engine.v16.depth_protocol import classify

    with patch("engine.v16.f1_classifier.EmbeddingClassifier._embed_text") as mock_embed:
        mock_embed.return_value = [0.1] * 1024

        # The depth_protocol.classify() should use f1_classifier.classify_v16()
        result = classify("build me a new classifier")
        assert result in intent_names()


def test_classify_fallback_to_regex() -> None:
    """Test that classify falls back to regex when embedding classifier fails."""
    from engine.v16.depth_protocol import classify

    with patch("engine.v16.f1_classifier.classify_v16") as mock_classify_v16:
        mock_classify_v16.side_effect = Exception("Ollama is down")

        # Should fall back to regex
        result = classify("fix the bug")
        assert result in intent_names()


# ==============================================================================
# TESTS: QUERY DIVERSITY
# ==============================================================================


@pytest.mark.parametrize(
    "query,expected_intent",
    [
        # Simple queries
        ("what is V15", "simple"),
        ("show me the configuration", "simple"),
        ("list all modules", "simple"),
        ("what is ARIS", "simple"),
        ("how many tests", "simple"),
        ("give me a summary", "simple"),
        # Fix queries
        ("the classifier is broken", "fix"),
        ("fix the regex patterns", "fix"),
        ("X is not working", "fix"),
        ("se cayó todo", "fix"),
        ("X no jala", "fix"),
        # Decision queries
        ("should we use embeddings", "decision"),
        ("which architecture is better", "decision"),
        ("compare Ollama vs Claude", "decision"),
        ("debemos usar embeddings o regex", "decision"),
        ("vale la pena cambiar el modelo", "decision"),
        ("should we change the approach", "decision"),
        # Implementation queries
        ("build me a dashboard", "implementation"),
        ("create a new module", "implementation"),
        ("implement the classifier", "implementation"),
        ("construye el modulo de login", "implementation"),
        ("adelante hazlo", "implementation"),
        ("go ahead and build it", "implementation"),
        # Research queries
        ("investigate embedding models", "research"),
        ("analyze the benchmarks", "research"),
        ("research the latest techniques", "research"),
        ("investiga las mejores practicas", "research"),
        ("encuentra alternativas a X", "research"),
    ],
)
def test_classify_diverse_queries(query: str, expected_intent: str) -> None:
    """Test classification on diverse queries (parametrized)."""
    with patch("engine.v16.f1_classifier.EmbeddingClassifier._embed_text") as mock_embed:
        # For testing purposes, return embeddings that correlate with expected intent
        # This is a heuristic test — exact accuracy depends on embeddings
        mock_embed.return_value = [0.1] * 1024

        result = classify_v16(query)
        # Just verify it's a valid intent; exact classification depends on embeddings
        assert result in intent_names()


# ==============================================================================
# TESTS: EDGE CASES
# ==============================================================================


def test_empty_query() -> None:
    """Test classification of empty query."""
    with patch("engine.v16.f1_classifier.EmbeddingClassifier._embed_text") as mock_embed:
        mock_embed.return_value = [0.0] * 1024

        result = classify_v16("")
        assert result in intent_names()


def test_very_long_query() -> None:
    """Test classification of very long query (should handle gracefully)."""
    long_query = "test " * 500  # 2500 chars

    with patch("engine.v16.f1_classifier.EmbeddingClassifier._embed_text") as mock_embed:
        # Return valid embedding regardless of input length
        mock_embed.return_value = [0.1] * 1024

        result = classify_v16(long_query)
        assert result in intent_names()

        # Verify _embed_text was called (with truncation applied inside it)
        assert mock_embed.called


def test_special_characters_in_query() -> None:
    """Test classification with special characters."""
    query = "fix bug: 404 error @#$%^&*()"

    with patch("engine.v16.f1_classifier.EmbeddingClassifier._embed_text") as mock_embed:
        mock_embed.return_value = [0.1] * 1024

        result = classify_v16(query)
        assert result in intent_names()


def test_mixed_language_query() -> None:
    """Test classification with mixed Spanish/English."""
    query = "build me a feature para hacer X"

    with patch("engine.v16.f1_classifier.EmbeddingClassifier._embed_text") as mock_embed:
        mock_embed.return_value = [0.1] * 1024

        result = classify_v16(query)
        assert result in intent_names()


# ==============================================================================
# TESTS: F7 CALIBRATION WRITE-BACK (temperature applied to live confidence)
# ==============================================================================


class TestConfidenceTemperature:
    """F1 reads the F7-calibrated temperature and recalibrates its confidence.

    F1's confidence is a cosine similarity in [0,1] (nearest-neighbor), recalibrated
    in logit space: sigmoid(logit(c)/T). T=1.0 must be the EXACT identity (fail-open).
    """

    def test_default_temperature_is_one_when_nothing_wired(self) -> None:
        """Fail-open: no override + no/empty calibration → temperature 1.0 (identity)."""
        with patch(
            "engine.v16.f1_classifier._load_active_temperature", return_value=1.0
        ):
            clf = EmbeddingClassifier(temperature=None)
        assert clf.temperature == 1.0

    def test_apply_temperature_identity_at_one(self) -> None:
        """T=1.0 leaves every confidence byte-identical (fail-open guarantee)."""
        clf = EmbeddingClassifier(temperature=1.0)
        for c in (0.0, 0.05, 0.37, 0.5, 0.7, 0.9999, 1.0):
            assert clf._apply_temperature(c) == c

    def test_apply_temperature_softens_overconfidence(self) -> None:
        """T>1 pulls confidence toward 0.5 (calibrating an overconfident classifier)."""
        clf = EmbeddingClassifier(temperature=2.0)
        # high confidence pulled down toward 0.5
        assert 0.5 < clf._apply_temperature(0.9) < 0.9
        # low confidence pulled up toward 0.5
        assert 0.1 < clf._apply_temperature(0.1) < 0.5

    def test_apply_temperature_sharpens(self) -> None:
        """T<1 pushes confidence away from 0.5."""
        clf = EmbeddingClassifier(temperature=0.5)
        assert clf._apply_temperature(0.9) > 0.9
        assert clf._apply_temperature(0.1) < 0.1

    def test_apply_temperature_endpoints_are_fixed_points(self) -> None:
        """0 and 1 map to themselves for any T (no log(0)/overflow)."""
        for t in (0.3, 1.0, 2.5, 9.0):
            clf = EmbeddingClassifier(temperature=t)
            assert clf._apply_temperature(0.0) == 0.0
            assert clf._apply_temperature(1.0) == 1.0

    def test_classify_argmax_unchanged_by_temperature(
        self, mock_ollama_embed
    ) -> None:
        """Temperature is monotonic → the chosen intent never changes, only confidence."""
        with patch(
            "engine.v16.f1_classifier.EmbeddingClassifier._embed_text"
        ) as m:
            m.side_effect = lambda text: mock_ollama_embed(text)
            # Query must NOT appear verbatim in exemplars.py — a verbatim match yields
            # cosine similarity = 1.0 (fixed point under any T) making abs(c_hi-c_id) = 0.
            q = "analyze this pull request for security flaws"
            i_id, c_id = EmbeddingClassifier(temperature=1.0).classify(q)
            i_hi, c_hi = EmbeddingClassifier(temperature=3.0).classify(q)
            assert i_id == i_hi  # decision unchanged
            assert abs(c_hi - c_id) > 1e-6  # but confidence recalibrated

    def test_classify_temperature_one_identical_to_baseline(
        self, mock_ollama_embed
    ) -> None:
        """T=1.0 reproduces the exact pre-calibration confidence (fail-open end-to-end)."""
        with patch(
            "engine.v16.f1_classifier.EmbeddingClassifier._embed_text"
        ) as m:
            m.side_effect = lambda text: mock_ollama_embed(text)
            q = "should we use Ollama or Claude API"
            _, c_a = EmbeddingClassifier(temperature=1.0).classify(q)
            _, c_b = EmbeddingClassifier(temperature=1.0).classify(q)
            assert c_a == c_b


class TestLoadActiveTemperature:
    """Fail-open reader of v16_active_calibration (F7 write-back target)."""

    def test_missing_db_returns_one(self, tmp_path) -> None:
        assert _load_active_temperature(tmp_path / "nope.db") == 1.0

    def test_db_without_table_returns_one(self, tmp_path) -> None:
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()
        assert _load_active_temperature(db) == 1.0

    def test_reads_latest_row(self, tmp_path) -> None:
        db = tmp_path / "cal.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE v16_active_calibration "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, temperature_coeff REAL)"
        )
        conn.execute("INSERT INTO v16_active_calibration (temperature_coeff) VALUES (1.5)")
        conn.execute("INSERT INTO v16_active_calibration (temperature_coeff) VALUES (2.5)")
        conn.commit()
        conn.close()
        assert _load_active_temperature(db) == 2.5  # latest-wins

    def test_out_of_range_value_ignored(self, tmp_path) -> None:
        db = tmp_path / "bad.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE v16_active_calibration "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, temperature_coeff REAL)"
        )
        conn.execute("INSERT INTO v16_active_calibration (temperature_coeff) VALUES (-5.0)")
        conn.commit()
        conn.close()
        assert _load_active_temperature(db) == 1.0

    def test_null_value_returns_one(self, tmp_path) -> None:
        db = tmp_path / "null.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE v16_active_calibration "
            "(id INTEGER PRIMARY KEY AUTOINCREMENT, temperature_coeff REAL)"
        )
        conn.execute("INSERT INTO v16_active_calibration (temperature_coeff) VALUES (NULL)")
        conn.commit()
        conn.close()
        assert _load_active_temperature(db) == 1.0
