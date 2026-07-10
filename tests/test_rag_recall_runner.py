"""Unit tests de la lógica PURA del benchmark RAG (evals/run_rag_recall.py).

Solo las funciones de scoring/derivación de query — sin Ollama ni DB → corren en CI.
Protegen las métricas de recall contra regresiones silenciosas.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_RUNNER = Path(__file__).resolve().parents[1] / "evals" / "run_rag_recall.py"
_spec = importlib.util.spec_from_file_location("rag_recall_runner", _RUNNER)
rr = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]  # spec_from_file_location can return None; test asserts file exists
_spec.loader.exec_module(rr)  # type: ignore[union-attr]  # loader is set for file-based specs


def test_query_from_text_exact_returns_full() -> None:
    assert (
        rr._query_from_text("uno dos tres cuatro", n_words=2, mode="exact") == "uno dos tres cuatro"
    )


def test_query_from_text_partial_truncates_to_n_words() -> None:
    assert (
        rr._query_from_text("uno dos tres cuatro cinco", n_words=3, mode="partial")
        == "uno dos tres"
    )


def test_query_from_text_partial_shorter_than_n_is_whole() -> None:
    assert rr._query_from_text("uno dos", n_words=10, mode="partial") == "uno dos"


def _hit(source: str, source_id: str) -> dict:
    return {"source": source, "source_id": source_id}


def test_rank_strict_found_at_position() -> None:
    hits = [_hit("observations", "a"), _hit("decisions", "x"), _hit("observations", "b")]
    assert rr._rank_strict(hits, "decisions", "x") == 2


def test_rank_strict_matches_by_str_coercion() -> None:
    # source_id puede llegar como int desde el vec store; el match coacciona a str.
    hits = [_hit("decisions", "42")]
    assert rr._rank_strict(hits, "decisions", 42) == 1


def test_rank_strict_absent_is_none() -> None:
    hits = [_hit("observations", "a"), _hit("observations", "b")]
    assert rr._rank_strict(hits, "decisions", "x") is None


def test_rank_strict_empty_hits_is_none() -> None:
    assert rr._rank_strict([], "observations", "a") is None
