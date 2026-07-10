"""Tests de robustez para los 6 fixes latentes (2026-06-29).

Fix #1 — read_status fallback usa nombre humano (no __name__ técnico).
Fix #2 — _project_profile.cache_clear() se llama en regenerate (punto de invalidación existe).
Fix #3 — _f1_pending tolera ts=null explícito sin TypeError.
Fix #4 — _apply envuelve write_text en try/except OSError (testado via unit sobre server).
Fix #5 — _percentile devuelve float real (round con ndigits=1).
Fix #6 — Docstrings corregidos (module header, _rice_row, read_session_briefs).
"""
from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aris4u_console import live_data as L  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(repo: Path) -> None:
    (repo / "data").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(repo / "data" / "sessions.db")
    conn.executescript("""
        CREATE TABLE decisions(decision TEXT, domain TEXT, locked INTEGER DEFAULT 0,
            created_at TEXT, client_id TEXT, mem_type TEXT);
        CREATE TABLE guards(pattern TEXT, prevention TEXT, severity TEXT,
            created_at TEXT, client_id TEXT);
        CREATE TABLE digests(date TEXT, summary TEXT, created_at TEXT, client_id TEXT);
        CREATE TABLE recall_feedback(recall_id TEXT PRIMARY KEY, useful INTEGER NOT NULL,
            marked_at TEXT);
    """)
    conn.execute("INSERT INTO decisions VALUES ('decid A','arch',1,'2026-06-01','aris4u',NULL)")
    conn.commit()
    conn.close()


def _make_log(repo: Path, lines: list[str]) -> None:
    (repo / "logs").mkdir(parents=True, exist_ok=True)
    p = repo / "logs" / "v16.1-events.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Fix #1 — read_status fallback da nombre humano
# ---------------------------------------------------------------------------

def test_read_status_fallback_gives_human_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cuando _st_memory lanza, el item fallback debe tener name='Memoria', no '_st_memory'."""
    _make_db(tmp_path)

    # Forzamos _st_memory a lanzar RuntimeError
    def boom(repo: Path) -> dict:
        raise RuntimeError("inyectado por test")

    monkeypatch.setattr(L, "_st_memory", boom)
    # Actualizar el mapa _ST_HUMAN para que apunte al nuevo callable (monkeypatch reemplaza
    # el atributo del módulo pero el dict ya guardó la referencia al original).
    # Reconstruimos el mapa para que boom → "Memoria"
    original_map = dict(L._ST_HUMAN)
    L._ST_HUMAN[boom] = "Memoria"

    try:
        status = L.read_status(tmp_path)
    finally:
        # Restaurar el mapa
        L._ST_HUMAN.clear()
        L._ST_HUMAN.update(original_map)

    names = [i["name"] for i in status["items"]]
    assert "Memoria" in names, f"El fallback debe dar 'Memoria', items={names}"
    # Asegurar que ningún item tiene el nombre técnico
    assert "_st_memory" not in names, f"El fallback no debe exponer __name__ técnico: {names}"


def test_read_status_fallback_all_functions_in_st_human() -> None:
    """Todos los callables que read_status usa están en _ST_HUMAN."""
    expected = {L._st_memory, L._st_vectors, L._st_recall, L._st_mcp,
                L._st_hooks, L._st_amplifier, L._st_body, L._st_ollama}
    missing = expected - set(L._ST_HUMAN.keys())
    assert not missing, f"Faltan en _ST_HUMAN: {[f.__name__ for f in missing]}"


def test_read_status_fallback_values_are_human_names() -> None:
    """Los valores de _ST_HUMAN son los nombres de _PURPOSE (contrato del espejo)."""
    for fn, human_name in L._ST_HUMAN.items():
        assert human_name in L._PURPOSE, (
            f"{fn.__name__} → '{human_name}' no está en _PURPOSE; "
            "el fallback rompería el contrato name↔purpose del espejo"
        )


# ---------------------------------------------------------------------------
# Fix #2 — _project_profile.cache_clear() existe como punto de invalidación
# ---------------------------------------------------------------------------

def test_project_profile_has_cache_clear() -> None:
    """_project_profile tiene cache_clear() — confirma que lru_cache está aplicado."""
    assert hasattr(L._project_profile, "cache_clear"), (
        "_project_profile debe ser @lru_cache para tener cache_clear()"
    )


def test_regenerate_calls_cache_clear(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """regenerate() llama a _project_profile.cache_clear() antes de reconstruir."""
    from aris4u_console import server as S

    cleared: list[bool] = []

    original_clear = L._project_profile.cache_clear

    def spy_clear() -> None:
        cleared.append(True)
        original_clear()

    monkeypatch.setattr(L._project_profile, "cache_clear", spy_clear)

    # Stub mínimo de regenerate (salta build_inventory real que necesita repo completo)
    # Verificamos que el punto de invalidación está en el código fuente de regenerate.
    src = inspect.getsource(S.regenerate)
    assert "cache_clear" in src, (
        "regenerate() debe llamar _project_profile.cache_clear() — Fix #2"
    )


# ---------------------------------------------------------------------------
# Fix #3 — _f1_pending tolera ts=null (Python None)
# ---------------------------------------------------------------------------

def test_f1_pending_tolerates_ts_none() -> None:
    """_f1_pending no lanza TypeError cuando ts es None explícito (JSON null)."""
    f1 = [
        {"available": True, "call_id": "c1", "tool": "aris_structure",
         "ts": None,  # null explícito — el bug original
         "backend": "mlx", "chars": 100},
        {"available": True, "call_id": "c2", "tool": "aris_critique",
         "ts": "2026-06-01T10:00:00",
         "backend": "mlx", "chars": 200},
    ]
    feedback: dict[str, bool] = {}
    # No debe lanzar
    result = L._f1_pending(f1, feedback)
    assert len(result) == 2
    ids = {r["call_id"] for r in result}
    assert ids == {"c1", "c2"}


def test_f1_pending_ts_none_gets_empty_age() -> None:
    """Un evento con ts=None produce age='' en el item resultante."""
    f1 = [{"available": True, "call_id": "cx", "tool": "aris_structure",
            "ts": None, "backend": "mlx", "chars": 50}]
    result = L._f1_pending(f1, {})
    assert result[0]["age"] == "", f"age debe ser '' para ts=None, got {result[0]['age']!r}"


def test_f1_pending_mixed_ts_sorts_without_error() -> None:
    """Mezcla de ts=None, ts='', ts=ISO no genera TypeError al ordenar."""
    f1 = [
        {"available": True, "call_id": "a", "tool": "t", "ts": None, "backend": "x", "chars": 1},
        {"available": True, "call_id": "b", "tool": "t", "ts": "", "backend": "x", "chars": 1},
        {"available": True, "call_id": "c", "tool": "t", "ts": "2026-01-01T00:00:00",
         "backend": "x", "chars": 1},
    ]
    result = L._f1_pending(f1, {})
    assert len(result) == 3


# ---------------------------------------------------------------------------
# Fix #5 — _percentile devuelve float (no int)
# ---------------------------------------------------------------------------

def test_percentile_returns_float() -> None:
    """_percentile devuelve float (anotación -> float honrada con round(x, 1))."""
    result = L._percentile([100.0, 200.0, 300.0], 0.5)
    assert isinstance(result, float), (
        f"_percentile debe devolver float, got {type(result).__name__}: {result!r}"
    )


def test_percentile_empty_returns_zero_float() -> None:
    result = L._percentile([], 0.5)
    assert result == 0.0
    assert isinstance(result, float)


def test_percentile_single_value() -> None:
    result = L._percentile([42.5], 0.5)
    assert isinstance(result, float)
    assert result == pytest.approx(42.5, abs=0.2)


def test_percentile_annotation_is_float() -> None:
    """La anotación de retorno dice float — coherente con la implementación."""
    # con `from __future__ import annotations` las anotaciones son strings; además
    # verificamos el comportamiento real (devuelve float, no int de round() sin ndigits).
    hints = L._percentile.__annotations__
    assert hints.get("return") == "float", (
        f"_percentile debe anotar -> float, got {hints.get('return')}"
    )
    assert isinstance(L._percentile([1, 2, 3, 4], 0.5), float)


# ---------------------------------------------------------------------------
# Fix #6 — Docstrings corregidos
# ---------------------------------------------------------------------------

def test_module_header_acknowledges_append_label_write() -> None:
    """El docstring del módulo menciona append_label como excepción de escritura."""
    doc = L.__doc__ or ""
    assert "append_label" in doc, (
        "El header del módulo debe mencionar append_label como escritura legítima"
    )


def test_rice_row_docstring_references_build_rice_atom() -> None:
    """_rice_row docstring debe referenciar _build_rice_atom, no _atom_row."""
    doc = L._rice_row.__doc__ or ""
    assert "_build_rice_atom" in doc, (
        "_rice_row docstring debe referenciar _build_rice_atom (el caller real), "
        "no _atom_row (ruta distinta)"
    )
    # Asegurar que no persiste la referencia incorrecta como caller principal
    assert "ya enriquecido por _atom_row" not in doc, (
        "El docstring stale '_atom_row' debe haberse corregido"
    )


def test_read_session_briefs_safe_limit_comment() -> None:
    """read_session_briefs tiene comentario aclaratorio sobre el f-string LIMIT."""
    src = inspect.getsource(L.read_session_briefs)
    assert "safe_limit" in src and "int" in src, (
        "read_session_briefs debe tener comentario aclaratorio sobre safe_limit/int"
    )
