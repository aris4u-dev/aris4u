"""WS3 — tests for the sqlite-vec sidecar vector store (engine.v16.vector_store).

Deterministic: query/item embeddings are monkeypatched so tests never touch Ollama.
The per-client isolation test covers the WS3 HIGH-severity risk (cross-client leak).
"""

import pytest

from engine.v16 import vector_store as vs

DIM = vs.EMBED_DIM


def _vec(*nonzero: tuple[int, float]) -> list[float]:
    """Build a DIM-length vector with the given (index, value) pairs set."""
    v = [0.0] * DIM
    for i, val in nonzero:
        v[i] = float(val)
    return v


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated sidecar DB in tmp_path; skips if sqlite-vec cannot load."""
    monkeypatch.setattr(vs, "ARIS_VECTORS_DB", tmp_path / "aris_vectors.db")
    vs._available = None
    if not vs.available():
        pytest.skip("sqlite-vec extension not available in this interpreter")
    assert vs.init_store() is True
    return vs


def test_init_empty(store) -> None:
    stats = store.get_stats()
    assert stats["available"] is True
    assert stats["total"] == 0


def test_per_client_isolation(store) -> None:
    """KNN filtered by client_id must NEVER return another client's rows."""
    user_a = _vec((0, 1.0))
    client_d_b = _vec((1, 1.0))           # orthogonal to user_a
    user_a2 = _vec((0, 0.9), (2, 0.1))  # near user_a
    assert store._upsert("observations", "A", user_a, client_id="user-a") == "indexed"
    assert store._upsert("observations", "B", client_d_b, client_id="client-d") == "indexed"
    assert store._upsert("observations", "A2", user_a2, client_id="user-a") == "indexed"

    # All queries resolve to the user_a direction (axis 0).
    store_embed = lambda text, role='doc': _vec((0, 1.0))  # noqa: E731

    import engine.v16.vector_store as mod
    mod._embed = store_embed

    user_a_hits = store.search("q", client_id="user-a", k=5)
    ids = {h["source_id"] for h in user_a_hits}
    assert ids == {"A", "A2"}, f"user-a leak/miss: {ids}"
    assert all(h["client_id"] == "user-a" for h in user_a_hits)

    client_d_hits = store.search("q", client_id="client-d", k=5)
    assert {h["source_id"] for h in client_d_hits} == {"B"}

    global_hits = store.search("q", client_id=None, k=5)
    assert {h["source_id"] for h in global_hits} == {"A", "A2", "B"}
    # closest global hit is the exact user_a match (cosine similarity ~1.0)
    assert global_hits[0]["source_id"] == "A"
    assert global_hits[0]["similarity"] > 0.99


def test_idempotency_and_reindex(store, monkeypatch) -> None:
    monkeypatch.setattr(vs, "_embed", lambda text, role='doc': _vec((0, 1.0)))
    assert vs.index_item("decisions", "42", "use sqlite-vec", client_id="user-a") == "indexed"
    assert vs.index_item("decisions", "42", "use sqlite-vec", client_id="user-a") == "skipped"
    assert vs.index_item("decisions", "42", "use pgvector instead", client_id="user-a") == "updated"
    assert store.get_stats()["total"] == 1  # still one row after reindex


def test_soft_scope_named_client_sees_unowned_never_other(store, monkeypatch) -> None:
    """Soft-scoping (FREEZE fix): a named client sees its OWN + UNOWNED vectors, but
    NEVER another client's (cross-client invariant A7 preserved). This corrects the
    earlier strict behavior that returned 0 for named clients over an unlabeled corpus
    (84% of recalls came back empty in real client projects)."""
    monkeypatch.setattr(vs, "_embed", lambda text, role='doc': _vec((0, 1.0)))
    assert vs.index_item("observations", "u1", "unscoped fact") == "indexed"  # sentinel ""
    assert vs.index_item("observations", "k1", "lab-project-1 fact", client_id="lab-project-1") == "indexed"
    assert vs.index_item("observations", "a1", "client-c fact", client_id="client-c") == "indexed"

    # lab-project-1 VE su vector + el sin-dueño; JAMÁS el de client-c.
    lab1_ids = {h["source_id"] for h in vs.search("q", client_id="lab-project-1", k=5)}
    assert lab1_ids == {"k1", "u1"}, f"soft-scope lab-project-1 leak/miss: {lab1_ids}"
    assert "a1" not in lab1_ids  # cross-client isolation intacto

    # client-c, simétrico: su vector + sin-dueño, nunca el de lab-project-1.
    clientc_ids = {h["source_id"] for h in vs.search("q", client_id="client-c", k=5)}
    assert clientc_ids == {"a1", "u1"}, f"soft-scope client-c leak/miss: {clientc_ids}"
    assert "k1" not in clientc_ids

    # client_id="" (sin dueño explícito) trae solo los sin-dueño.
    unscoped = vs.search("q", client_id="", k=5)
    assert {h["source_id"] for h in unscoped} == {"u1"}


def test_graceful_degradation_when_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(vs, "ARIS_VECTORS_DB", tmp_path / "x.db")
    monkeypatch.setattr(vs, "_available", False)
    assert vs.search("q") == []
    assert vs.index_item("observations", "1", "text") == "unavailable"
    assert vs.get_stats()["available"] is False
