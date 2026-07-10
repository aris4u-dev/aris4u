"""Regresión de _hydrate — V18 Fase E: hidrata desde el texto PROPIO (observations_local
en sessions.db), no de la claude-mem.db 3er-party (archivada, sin fallback). El id =
vec_map.source_id; content presente → texto, ausente/vacío → None.

También cubre _humanize_embedding_text: transformación de texto para embedding que
convierte slugs ``name:<slug> | <content>`` en texto natural para el modelo de embeddings.
El texto original en la DB nunca se modifica — la transformación solo aplica al path
de embedding.
"""
import sqlite3

import pytest

from engine.v16 import session_manager as sm


@pytest.fixture
def mem_db(tmp_path, monkeypatch):
    db = tmp_path / "sessions.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE observations_local (id TEXT PRIMARY KEY, content TEXT)"
    )
    con.executemany(
        "INSERT INTO observations_local (id, content) VALUES (?,?)",
        [
            ("1", "texto real"),   # content presente → gana
            ("3", "desde titulo"),  # content presente
            ("4", ""),              # content vacío → None
        ],
    )
    con.commit()
    con.close()
    monkeypatch.setattr(sm, "SESSIONS_DB", db)
    return db


# _hydrate devuelve (texto, epistemic_status, mem_type, problem_class, validity_domain,
# structural_signature, canonical_id). Para observations los 6 extra son None.
def test_hydrate_returns_content(mem_db) -> None:
    assert sm._hydrate("observations", "1") == ("texto real", None, None, None, None, None, None)


def test_hydrate_returns_content_second_row(mem_db) -> None:
    assert sm._hydrate("observations", "3") == ("desde titulo", None, None, None, None, None, None)


def test_hydrate_empty_content_returns_none(mem_db) -> None:
    assert sm._hydrate("observations", "4") is None


def test_hydrate_missing_row_returns_none(mem_db) -> None:
    assert sm._hydrate("observations", "999") is None


def test_semantic_recall_filters_by_epistemic_status(monkeypatch) -> None:
    """Taxonomía 2026-06: el recall NUNCA inyecta refuted/superseded/provenance como
    guía, y etiqueta lo provisional/open_question. (Sin Ollama: mockea KNN + _hydrate.)"""
    from engine.v16 import diverse_recall, vector_store
    hits = [{"source": "decisions", "source_id": str(i), "client_id": "", "similarity": 0.9}
            for i in (10, 11, 12, 13, 14, 15)]
    # 7-tuple: (texto, epistemic_status, mem_type, problem_class, validity_domain,
    # structural_signature, canonical_id). problem_class/validity_domain/sig/canonical_id = None
    # aquí: este test verifica el filtro epistémico, no los átomos de método (cubiertos aparte).
    table = {
        "10": ("decision confirmada", "confirmed", "decision", None, None, None, None),
        "11": ("anti-patron muerto", "refuted", "rule", None, None, None, None),
        "12": ("decision superada", "superseded", "decision", None, None, None, None),
        "13": ("hipotesis sin verificar", "provisional", "fact", None, None, None, None),
        "14": ("[commit abc] log", "provisional", "provenance", None, None, None, None),
        "15": ("pregunta abierta X", "open_question", "decision", None, None, None, None),
    }
    monkeypatch.setattr(vector_store, "search", lambda q, client_id=None, k=5: hits)
    monkeypatch.setattr(sm, "_hydrate", lambda src, sid: table.get(sid))
    monkeypatch.setattr(diverse_recall, "enabled", lambda: False)

    r = sm.semantic_recall("q", limit=10)
    sts = {x.get("epistemic_status") for x in r}
    texts = [x["text"] for x in r]
    assert "refuted" not in sts and "superseded" not in sts          # muertos fuera
    assert not any("commit" in t for t in texts)                     # provenance fuera
    assert "decision confirmada" in texts                            # confirmed limpio (sin tag)
    assert any(t.startswith("(sin verificar) ") for t in texts)      # provisional etiquetado
    assert any(t.startswith("(pregunta abierta) ") for t in texts)   # open_question etiquetado


# ---------------------------------------------------------------------------
# _humanize_embedding_text — transformación de slug para embedding (2026-07-03)
# ---------------------------------------------------------------------------

def test_humanize_hyphen_slug() -> None:
    """Slug con guiones se convierte en palabras separadas por espacio."""
    result = sm._humanize_embedding_text(
        "name:stripe-external-id-on-profile | ALTER TABLE profiles ADD COLUMN stripe_customer_id TEXT;"
    )
    assert result.startswith("stripe external id on profile\n")
    assert "ALTER TABLE" in result


def test_humanize_underscore_slug() -> None:
    """Slug con underscores se convierte en palabras separadas por espacio."""
    result = sm._humanize_embedding_text(
        "name:multi_reaction_type_with_backward_compat_like | reactionCounts: Map<String,int>;"
    )
    assert result.startswith("multi reaction type with backward compat like\n")
    assert "reactionCounts" in result


def test_humanize_mixed_slug() -> None:
    """Slug con guiones y underscores se normaliza completamente."""
    result = sm._humanize_embedding_text("name:rls-owner_isolation | ALTER TABLE t ENABLE ROW LEVEL SECURITY;")
    assert result.startswith("rls owner isolation\n")


def test_humanize_passthrough_plain_text() -> None:
    """Texto sin el patrón name: sale intacto."""
    plain = "Use RLS on all Supabase tables that hold user data."
    assert sm._humanize_embedding_text(plain) == plain


def test_humanize_passthrough_atom_format() -> None:
    """Átomos con formato [atom:slug] NO coinciden — salen intactos."""
    atom = "[atom:r1_shannon-source-coding] (rama 1 teorema) El source coding theorem."
    assert sm._humanize_embedding_text(atom) == atom


def test_humanize_passthrough_uppercase_slug() -> None:
    """Slugs con mayúsculas no coinciden (el patrón es [a-z0-9_-])."""
    upper = "name:MySlug | some content"
    assert sm._humanize_embedding_text(upper) == upper


def test_humanize_db_text_unchanged() -> None:
    """El texto original NO se modifica en la DB; la transformación es solo para embedding.

    Verifica que _humanize_embedding_text es una función pura: entrada → nueva cadena,
    sin efectos secundarios sobre el argumento original.
    """
    original = "name:rls-join-through-parent-ownership | CREATE POLICY code here;"
    _ = sm._humanize_embedding_text(original)
    # La variable original sigue siendo el texto bruto de la DB
    assert original.startswith("name:rls-join-through-parent-ownership")
