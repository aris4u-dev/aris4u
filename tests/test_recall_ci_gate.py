"""CI gate — RAG recall@1 threshold (P2-A).

Runs the known-item search benchmark (evals/run_rag_recall.py) via subprocess
and asserts content recall@1 >= 0.88 against the live vector index.

Design rationale: subprocess call avoids the conftest autouse fixture
``_isolate_sessions_db`` which redirects sessions.db to a temp DB and would
make ``_rank_content``/``_hydrate`` see an empty store (recall 0). The
subprocess uses the real data/sessions.db and data/aris_vectors.db.

Baseline (2026-07-03, 9261 vectors, n=200):
  content recall@1 (exact mode) = 0.9246
  strict recall@1 (exact mode)  = 0.6884

Threshold = 0.88 (−4 pp below baseline). Strict recall is intentionally
excluded: at 0.69 it would always fail this gate.

Skip conditions (evaluated at collection time):
  - Ollama Mac not responding on :11434
  - aris_vectors.db missing or empty
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_RUNNER = ROOT / "evals" / "run_rag_recall.py"
_PYTHON = ROOT / ".venv312" / "bin" / "python3"
_VECTORS_DB = ROOT / "data" / "aris_vectors.db"

_RECALL_AT_1_MIN: float = 0.88  # content recall@1; baseline 0.9246


def _ollama_available() -> bool:
    """Probe Ollama Mac endpoint; returns False on any error or timeout."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:11434/api/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _vector_store_populated() -> bool:
    """Return True if aris_vectors.db exists and has at least one row in vec_map."""
    if not _VECTORS_DB.exists():
        return False
    try:
        import sqlite3
        con = sqlite3.connect(str(_VECTORS_DB))
        count = con.execute("SELECT COUNT(*) FROM vec_map").fetchone()[0]
        con.close()
        return count > 0
    except Exception:
        return False


@pytest.mark.skipif(
    not _ollama_available(),
    reason="Ollama no disponible — skip en CI sin embedder local",
)
@pytest.mark.skipif(
    not _vector_store_populated(),
    reason="aris_vectors.db vacío o inexistente — skip en entorno sin índice",
)
def test_rag_content_recall_at_1_meets_threshold() -> None:
    """content recall@1 (exact mode) >= 0.88 sobre el índice vectorial vivo.

    Samples 100 items deterministically from vec_map, derives exact queries
    (full text), and checks whether KNN k=10 retrieves the source item in the
    top-1 position by content match (text equality, deduplication-safe).

    A regression below 0.88 signals vector index degradation (stale embeddings,
    dimension mismatch, DB corruption) and must be investigated before release.
    """
    result = subprocess.run(
        [
            str(_PYTHON), str(_RUNNER),
            "--n", "100",
            "--words", "14",
            "--k", "10",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(ROOT),
    )
    if result.returncode == 2:
        # Script returned 2 = embedder down or vector store unavailable at runtime.
        pytest.skip(f"eval script skip (rc=2): {result.stderr.strip()[:300]}")

    assert result.returncode == 0, (
        f"eval script failed (rc={result.returncode}):\nSTDOUT: {result.stdout[:500]}"
        f"\nSTDERR: {result.stderr[:500]}"
    )

    report = json.loads(result.stdout)
    exact_mode = next(
        (r for r in report["results"] if r["mode"] == "exact"), None
    )
    assert exact_mode is not None, "mode='exact' ausente en el reporte JSON"

    recall_1 = exact_mode["recall_at_content"]["@1"]
    evaluated = exact_mode.get("evaluated", "?")
    assert recall_1 >= _RECALL_AT_1_MIN, (
        f"REGRESIÓN: content recall@1 = {recall_1:.4f} < umbral {_RECALL_AT_1_MIN} "
        f"(baseline=0.9246, evaluados={evaluated}, vectores={report.get('vectors_total', '?')})"
    )
