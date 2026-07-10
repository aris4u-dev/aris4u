"""
Tests for V16 novelty_detector.py — new-domain detection.

Uses mocked claude-mem (in-memory SQLite) and mocked embeddings to avoid
real Ollama dependency in tests.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from engine.v16.novelty_detector import (
    NoveltyResult,
    _cosine_similarity,
    _embed_text,
    _exemplar_distance,
    _match_atoms,
    _search_claude_mem,
    detect_novelty,
)


@pytest.fixture
def mock_embeddings():
    """Mock embedding function that returns deterministic vectors."""

    def embed_mock(text: str, model: str = "mxbai-embed-large"):
        # Deterministic hashing for reproducibility
        hash_val = hash(text) % 100
        # Create a simple vector based on hash
        vec = np.zeros(1024, dtype=np.float32)
        vec[0] = hash_val / 100.0
        vec[1] = (hash_val + 1) / 100.0
        # Normalize
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm
        return vec

    return embed_mock


@pytest.fixture
def mock_claude_mem_empty(tmp_path):
    """V18 Fase E: sessions.db vacía (observations_local_fts) — la novedad ya no lee claude-mem."""
    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE VIRTUAL TABLE observations_local_fts USING fts5(content)")
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def mock_claude_mem_with_data(tmp_path):
    """V18 Fase E: sessions.db con observations_local_fts poblada (texto propio)."""
    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE VIRTUAL TABLE observations_local_fts USING fts5(content)")
    conn.execute(
        """
        INSERT INTO observations_local_fts(content)
        VALUES
        ('V16 is the unified engine design with hooks and memory'),
        ('Depth protocol activates adaptive levels 1-10'),
        ('Knowledge atoms are condensed research')
        """
    )
    conn.commit()
    conn.close()
    return db_path


class TestCosineSimilarity:
    """Test _cosine_similarity helper."""

    def test_identical_vectors(self):
        a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        b = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        assert _cosine_similarity(a, b) == pytest.approx(1.0, abs=0.01)

    def test_orthogonal_vectors(self):
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([0.0, 1.0], dtype=np.float32)
        assert _cosine_similarity(a, b) == pytest.approx(0.0, abs=0.01)

    def test_zero_vector(self):
        a = np.zeros(10, dtype=np.float32)
        b = np.array([1.0] * 10, dtype=np.float32)
        assert _cosine_similarity(a, b) == 0.0

    def test_opposite_vectors(self):
        a = np.array([1.0, 0.0], dtype=np.float32)
        b = np.array([-1.0, 0.0], dtype=np.float32)
        sim = _cosine_similarity(a, b)
        assert sim == pytest.approx(0.0, abs=0.01) or sim < 0.1


class TestEmbedText:
    """Test _embed_text helper."""

    @patch("engine.v16.novelty_detector.subprocess.run")
    def test_successful_embedding(self, mock_run):
        """Test successful embedding via Ollama."""
        mock_run.return_value = MagicMock(
            stdout='{"embedding": [0.1, 0.2, 0.3]}',
            returncode=0,
        )
        result = _embed_text("test query")
        assert result is not None
        assert len(result) == 3
        assert isinstance(result, np.ndarray)

    @patch("engine.v16.novelty_detector.subprocess.run")
    def test_embedding_timeout(self, mock_run):
        """Test embedding timeout."""
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired("curl", 10)
        result = _embed_text("test query")
        assert result is None

    @patch("engine.v16.novelty_detector.subprocess.run")
    def test_embedding_invalid_json(self, mock_run):
        """Test embedding with invalid JSON response."""
        mock_run.return_value = MagicMock(stdout='invalid json')
        result = _embed_text("test query")
        assert result is None


class TestSearchClaudeMem:
    """Test _search_claude_mem helper."""

    @patch("engine.v16.novelty_detector.SESSIONS_DB")
    def test_search_no_db(self, mock_db_path):
        """Test search when DB doesn't exist."""
        mock_db_path.__str__ = lambda x: "/nonexistent/path.db"
        mock_db_path.exists.return_value = False
        hits, score = _search_claude_mem("test query")
        assert hits == 0
        assert score == 0.0

    def test_search_empty_db(self, mock_claude_mem_empty):
        """Test search in empty DB."""
        with patch("engine.v16.novelty_detector.SESSIONS_DB", mock_claude_mem_empty):
            hits, score = _search_claude_mem("blockchain smart contracts")
            assert hits == 0
            assert score == 0.0

    def test_search_with_data(self, mock_claude_mem_with_data):
        """Test search in DB with observations."""
        with patch("engine.v16.novelty_detector.SESSIONS_DB", mock_claude_mem_with_data):
            # Search for known topic (should find hits)
            hits, score = _search_claude_mem("V16 hooks")
            assert hits >= 1
            # Score is derived from BM25 rank (negative, converted to 0-1)
            assert 0.0 <= score <= 1.0


class TestMatchAtoms:
    """Test _match_atoms helper.

    V16.3 F3 fix: `_match_atoms` uses cached embeddings instead of calling
    `_embed_text` per atom. Tests patch the cache loader directly.
    """

    def test_atoms_match_high_sim(self):
        """Test atom matching with high similarity via patched cache."""
        # Cached atoms array — all atoms are the same vector so query matches
        atom_vec = np.array([0.1, 0.9] + [0.0] * 1022, dtype=np.float32)
        atom_vec = atom_vec / np.linalg.norm(atom_vec)
        # Reset module-level cache so patched loader actually runs
        import engine.v16.novelty_detector as nd
        nd._atoms_embeddings = None

        fake_cache = np.array([atom_vec] * 5, dtype=np.float32)
        with patch("engine.v16.novelty_detector._load_or_compute_atoms_cache", return_value=fake_cache):
            matched, max_sim = _match_atoms(atom_vec)
            assert matched == 5  # all 5 match (similarity = 1.0 > 0.4)
            assert max_sim > 0.99

    def test_atoms_match_no_atoms_data(self):
        """Test atom matching when cache is empty/missing."""
        import engine.v16.novelty_detector as nd
        nd._atoms_embeddings = None

        query_emb = np.array([0.1, 0.9] + [0.0] * 1022, dtype=np.float32)
        query_emb = query_emb / np.linalg.norm(query_emb)

        with patch("engine.v16.novelty_detector._load_or_compute_atoms_cache", return_value=None):
            matched, max_sim = _match_atoms(query_emb)
            assert matched == 0
            assert max_sim == 0.0


class TestExemplarDistance:
    """Test _exemplar_distance helper.

    V16.3 F3 fix: `_exemplar_distance` uses cached embeddings shared with
    f1_classifier. Tests patch the cache loader directly.
    """

    def test_exemplar_distance_same_as_exemplar(self):
        """Test distance when query matches an exemplar via patched cache."""
        import engine.v16.novelty_detector as nd
        nd._exemplars_embeddings = None

        query_vec = np.array([1.0, 0.0] + [0.0] * 1022, dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)

        # Cache contains the same vector as the query
        fake_cache = np.array([query_vec] * 10, dtype=np.float32)
        with patch("engine.v16.novelty_detector._load_or_compute_exemplars_cache", return_value=fake_cache):
            distance = _exemplar_distance(query_vec)
            assert distance < 0.01  # cosine sim = 1.0 → distance ≈ 0

    @patch("engine.v16.novelty_detector._embed_text")
    def test_exemplar_distance_orthogonal(self, mock_embed):
        """Test distance when query is orthogonal to all exemplars."""
        # Exemplars will all be [1, 0, ...]
        mock_embed.return_value = np.array([1.0, 0.0] + [0.0] * 1022, dtype=np.float32)

        # Query is [0, 1, ...]
        query_vec = np.array([0.0, 1.0] + [0.0] * 1022, dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)

        distance = _exemplar_distance(query_vec)
        # Should be near 1.0 (orthogonal)
        assert distance > 0.8


class TestDetectNovelty:
    """Test main detect_novelty function."""

    def test_empty_query(self):
        """Test novelty detection with empty query."""
        result = detect_novelty("")
        assert result.is_new_domain is False
        assert result.confidence == 0.0

    @patch("engine.v16.novelty_detector._embed_text")
    def test_known_aris_topic(self, mock_embed, mock_claude_mem_with_data):
        """Test query about known ARIS topic."""
        query_vec = np.array([0.5, 0.5] + [0.0] * 1022, dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)
        mock_embed.return_value = query_vec

        with patch("engine.v16.novelty_detector.SESSIONS_DB", mock_claude_mem_with_data):
            with patch("engine.v16.novelty_detector._match_atoms") as mock_atoms:
                with patch("engine.v16.novelty_detector._exemplar_distance") as mock_dist:
                    # Known topic: should have recall hits, atom matches, close exemplar
                    mock_atoms.return_value = (5, 0.7)  # 5 atoms matched, high sim
                    mock_dist.return_value = 0.3  # Close to exemplar

                    result = detect_novelty("How do hooks work in ARIS4U?")

                    assert result.is_new_domain is False
                    assert result.confidence < 0.5
                    assert result.recommended_depth_override is None

    @patch("engine.v16.novelty_detector._embed_text")
    def test_unknown_topic_blockchain(self, mock_embed, mock_claude_mem_empty):
        """Test query about unknown blockchain topic."""
        query_vec = np.array([0.9, 0.1] + [0.0] * 1022, dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)
        mock_embed.return_value = query_vec

        with patch("engine.v16.novelty_detector.SESSIONS_DB", mock_claude_mem_empty):
            with patch("engine.v16.novelty_detector._match_atoms") as mock_atoms:
                with patch("engine.v16.novelty_detector._exemplar_distance") as mock_dist:
                    # Unknown topic: 0 recall, 0 atoms, far exemplar
                    mock_atoms.return_value = (0, 0.1)  # No atoms matched
                    mock_dist.return_value = 0.95  # Far from exemplars

                    result = detect_novelty("How do I implement a Solidity smart contract?")

                    assert result.is_new_domain is True
                    assert result.confidence > 0.5
                    assert result.recommended_depth_override == "deep_exploration"

    @patch("engine.v16.novelty_detector._embed_text")
    def test_confidence_calculation(self, mock_embed):
        """Test confidence weighting (0.4 recall + 0.3 atoms + 0.3 exemplars)."""
        query_vec = np.array([0.5] * 1024, dtype=np.float32)
        query_vec = query_vec / np.linalg.norm(query_vec)
        mock_embed.return_value = query_vec

        with patch("engine.v16.novelty_detector.SESSIONS_DB") as mock_db:
            mock_db.exists.return_value = False
            with patch("engine.v16.novelty_detector._match_atoms") as mock_atoms:
                with patch("engine.v16.novelty_detector._exemplar_distance") as mock_dist:
                    mock_atoms.return_value = (0, 0.1)
                    mock_dist.return_value = 0.95

                    result = detect_novelty("Unknown topic")

                    # All 3 signals active:
                    # recall_signal=True (no hits), atoms_signal=True (sim<0.4), exemplars_signal=True (dist>0.6)
                    # confidence = 0.4*1 + 0.3*1 + 0.3*1 = 1.0
                    assert result.confidence == pytest.approx(1.0, abs=0.1)

    @patch("engine.v16.novelty_detector._embed_text")
    def test_embedding_failure_fallback(self, mock_embed):
        """Test fail-safe when embedding fails: assume novel for depth escalation.

        V16.3 fix (F8): when Ollama is down, assume novel rather than silently
        treating the query as familiar. Rationale: this module exists to
        escalate depth for unknown domains; reverting to shallow depth on
        embedding failure subverts the purpose.
        """
        mock_embed.return_value = None

        result = detect_novelty("Any query")

        assert result.is_new_domain is True
        assert result.confidence == pytest.approx(0.5, abs=0.01)
        assert result.recommended_depth_override == "deep_exploration"
        assert any("embedding failed" in r for r in result.reasons)


class TestNoveltyResultDataclass:
    """Test NoveltyResult dataclass."""

    def test_default_initialization(self):
        """Test NoveltyResult initialization."""
        result = NoveltyResult(
            is_new_domain=True,
            confidence=0.95,
        )
        assert result.is_new_domain is True
        assert result.confidence == 0.95
        assert result.reasons == []
        assert result.recall_hits == 0
        assert result.atoms_matched == 0
        assert result.exemplars_distance == 1.0
        assert result.recommended_depth_override is None

    def test_full_initialization(self):
        """Test NoveltyResult with all fields."""
        result = NoveltyResult(
            is_new_domain=True,
            confidence=0.8,
            reasons=["0 observations", "no atoms matched"],
            recall_hits=0,
            atoms_matched=0,
            exemplars_distance=0.75,
            recommended_depth_override="deep_exploration",
        )
        assert result.is_new_domain is True
        assert len(result.reasons) == 2
        assert result.recommended_depth_override == "deep_exploration"


class TestAtomsCacheCanary:
    """Gate canary anti-cambio-de-modelo en la caché PROPIA de atoms (atoms_embeddings.npz).

    Cierra el gap de riesgo ALTO del audit: sin canary, un cambio de modelo de embeddings
    en Ollama dejaba la caché stale en silencio (mismo modo-fallo que degradó el Depth Protocol).
    """

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        import engine.v16.novelty_detector as nd
        monkeypatch.setattr(nd, "_ATOMS_CACHE_PATH", tmp_path / "atoms_embeddings.npz")
        monkeypatch.setattr(nd, "_atoms_embeddings", None)
        self.nd = nd

    def test_cache_without_canary_is_stale(self):
        # Formato viejo (sin _canary) → se debe recomputar.
        cached = {"embeddings": np.zeros((2, 1024), dtype=np.float32)}
        assert self.nd._atoms_cache_is_fresh(cached) is False

    def test_canary_mismatch_invalidates(self, monkeypatch):
        # El modelo "cambió": el embed vivo es ortogonal al canary cacheado → sim 0 < 0.95.
        cached = {"_canary": np.array([1.0, 0.0] + [0.0] * 1022, dtype=np.float32)}
        monkeypatch.setattr(self.nd, "_embed_text",
                            lambda t: np.array([0.0, 1.0] + [0.0] * 1022, dtype=np.float32))
        assert self.nd._atoms_cache_is_fresh(cached) is False

    def test_canary_match_is_fresh(self, monkeypatch):
        vec = np.array([1.0, 0.0] + [0.0] * 1022, dtype=np.float32)
        monkeypatch.setattr(self.nd, "_embed_text", lambda t: vec.copy())
        assert self.nd._atoms_cache_is_fresh({"_canary": vec}) is True

    def test_ollama_down_trusts_cache(self, monkeypatch):
        # Ollama caído (embed None) → NO invalidar una caché posiblemente buena.
        monkeypatch.setattr(self.nd, "_embed_text", lambda t: None)
        cached = {"_canary": np.array([1.0] + [0.0] * 1023, dtype=np.float32)}
        assert self.nd._atoms_cache_is_fresh(cached) is True

    def test_canary_persisted_on_save(self, monkeypatch):
        vec = np.array([0.5] * 1024, dtype=np.float32)
        monkeypatch.setattr(self.nd, "_embed_text", lambda t: vec.copy())
        arr = self.nd._compute_and_cache_atoms()
        assert arr is not None
        loaded = np.load(self.nd._ATOMS_CACHE_PATH)
        assert "_canary" in loaded.files

    def test_no_persist_when_canary_embed_fails(self, monkeypatch):
        # Atoms embeben OK pero el canary falla → NO persistir (evita envenenar con ceros).
        def flaky(text):
            return None if text == self.nd._CANARY_TEXT else np.ones(1024, dtype=np.float32)
        monkeypatch.setattr(self.nd, "_embed_text", flaky)
        arr = self.nd._compute_and_cache_atoms()
        assert arr is not None  # devuelve en memoria
        assert not self.nd._ATOMS_CACHE_PATH.exists()  # pero NO escribe disco


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
