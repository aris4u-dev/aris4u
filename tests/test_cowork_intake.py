"""Tests para tools/cowork_intake.py.

Cubren:
- Migración idempotente (ensure_intake_table) + flag once-per-process (L1)
- create_intake: persiste brief + docs + fila en DB; devuelve (row_id, skipped_docs)
- list_intakes / get_intake / set_status
- Whitelist de extensión: rechaza tipos no permitidos (M1 — skipped_docs poblado)
- Cap de tamaño: rechaza docs que superan _MAX_DOC_BYTES (M1)
- Cap de número de docs: max _MAX_DOCS_PER_INTAKE (H1)
- Validación de client_id: allowlist ^[a-z0-9_-]+$ (M2)
- Aislamiento por client_id
- Docs con nombre inválido (path-traversal, vacío, oculto) omitidos

Todos los fixtures usan tmp_path y sqlite temporal; nunca tocan sessions.db viva.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tools.cowork_intake import (  # noqa: E402
    _ALLOWED_DOC_EXTS,
    _MAX_DOC_BYTES,
    _MAX_DOCS_PER_INTAKE,
    _VALID_STATUSES,
    create_intake,
    ensure_intake_table,
    get_intake,
    list_intakes,
    set_status,
)
import tools.cowork_intake as _ci_mod  # noqa: E402  (for resetting module-level flag)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create(db: Path, data_dir: Path, client: str = "testceo",
            brief: str = "brief", docs: list[dict] | None = None) -> tuple[int, list[str]]:
    """Wrapper de create_intake con defaults para reducir repetición."""
    return create_intake(
        db_path=db, client_id=client, brief_text=brief,
        doc_files=docs or [], data_dir=data_dir,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """DB SQLite temporal aislada; la tabla la crea create_intake."""
    # Reset the once-per-process flag so each test gets a fresh table check.
    _ci_mod._INTAKE_TABLE_READY = False
    return tmp_path / "test_sessions.db"


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """Directorio de datos temporal separado de la DB."""
    d = tmp_path / "data"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# ensure_intake_table + L1 (once-per-process flag)
# ---------------------------------------------------------------------------

class TestEnsureIntakeTable:
    def test_creates_table(self, db: Path) -> None:
        ensure_intake_table(db)
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='intake_requests'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "tabla intake_requests no creada"

    def test_idempotent(self, db: Path) -> None:
        """Llamar dos veces no debe arrojar error ni duplicar la tabla."""
        ensure_intake_table(db)
        ensure_intake_table(db)  # segunda llamada silenciosa (flag ya True)
        conn = sqlite3.connect(str(db))
        try:
            count = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='intake_requests'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_flag_set_after_first_call(self, db: Path) -> None:
        """L1: tras la primera llamada, _INTAKE_TABLE_READY debe ser True."""
        assert not _ci_mod._INTAKE_TABLE_READY  # fixture resetea a False
        ensure_intake_table(db)
        assert _ci_mod._INTAKE_TABLE_READY


# ---------------------------------------------------------------------------
# create_intake — happy path
# ---------------------------------------------------------------------------

class TestCreateIntakeHappyPath:
    def test_returns_tuple_row_id_and_skipped(self, db: Path, data_dir: Path) -> None:
        result = _create(db, data_dir)
        assert isinstance(result, tuple) and len(result) == 2
        row_id, skipped = result
        assert isinstance(row_id, int) and row_id >= 1
        assert skipped == []

    def test_brief_written_to_disk(self, db: Path, data_dir: Path) -> None:
        brief = "Necesito una app de agenda medica"
        _create(db, data_dir, brief=brief)
        intake_dirs = list((data_dir / "intake").iterdir())
        assert len(intake_dirs) == 1
        brief_file = intake_dirs[0] / "brief.md"
        assert brief_file.exists()
        assert brief_file.read_text(encoding="utf-8") == brief

    def test_row_persisted_in_db(self, db: Path, data_dir: Path) -> None:
        row_id, _ = _create(db, data_dir)
        item = get_intake(db, row_id)
        assert item is not None
        assert item["client_id"] == "testceo"
        assert item["status"] == "pending"
        assert item["brief_path"].endswith("brief.md")

    def test_doc_written_to_disk(self, db: Path, data_dir: Path) -> None:
        content = b"# Requisitos\n- item1\n"
        _create(db, data_dir, docs=[{"name": "requisitos.md", "content": content}])
        intake_dirs = list((data_dir / "intake").iterdir())
        doc_file = intake_dirs[0] / "docs" / "requisitos.md"
        assert doc_file.exists()
        assert doc_file.read_bytes() == content

    def test_multiple_valid_docs(self, db: Path, data_dir: Path) -> None:
        docs = [
            {"name": "spec.txt", "content": b"spec content"},
            {"name": "data.csv", "content": b"col1,col2\n1,2\n"},
        ]
        _create(db, data_dir, docs=docs)
        intake_dirs = list((data_dir / "intake").iterdir())
        docs_dir = intake_dirs[0] / "docs"
        assert (docs_dir / "spec.txt").exists()
        assert (docs_dir / "data.csv").exists()


# ---------------------------------------------------------------------------
# M2 — validación de client_id
# ---------------------------------------------------------------------------

class TestClientIdValidation:
    def test_empty_client_raises(self, db: Path, data_dir: Path) -> None:
        with pytest.raises(ValueError, match="client_id"):
            _create(db, data_dir, client="")

    def test_uppercase_normalizes_to_valid(self, db: Path, data_dir: Path) -> None:
        """Uppercase input is lowercased before validation — 'MyClient' → 'myclient' → valid."""
        row_id, _ = _create(db, data_dir, client="MyClient")
        item = get_intake(db, row_id)
        assert item is not None and item["client_id"] == "myclient"

    def test_spaces_raises(self, db: Path, data_dir: Path) -> None:
        with pytest.raises(ValueError, match="inválido"):
            _create(db, data_dir, client="my client")

    def test_sql_injection_attempt_raises(self, db: Path, data_dir: Path) -> None:
        with pytest.raises(ValueError, match="inválido"):
            _create(db, data_dir, client='gate"; DROP TABLE intake_requests--')

    def test_valid_formats_accepted(self, db: Path, data_dir: Path) -> None:
        """Formatos válidos: lowercase, dígitos, guion, guion_bajo."""
        for client in ("client-c", "my-client", "client_2", "a1b2c3", "aris4u"):
            _ci_mod._INTAKE_TABLE_READY = False
            row_id, _ = _create(db, data_dir, client=client)
            assert row_id >= 1
            item = get_intake(db, row_id)
            assert item is not None and item["client_id"] == client


# ---------------------------------------------------------------------------
# H1 — cap de número de docs
# ---------------------------------------------------------------------------

class TestDocCountCap:
    def test_cap_enforced(self, db: Path, data_dir: Path) -> None:
        """30 docs enviados → solo _MAX_DOCS_PER_INTAKE escritos; el resto en skipped."""
        total = _MAX_DOCS_PER_INTAKE + 5
        docs = [{"name": f"doc{i}.txt", "content": b"x"} for i in range(total)]
        _, skipped = _create(db, data_dir, docs=docs)
        intake_dirs = list((data_dir / "intake").iterdir())
        written = list((intake_dirs[0] / "docs").iterdir())
        assert len(written) == _MAX_DOCS_PER_INTAKE
        assert len(skipped) == 5

    def test_exactly_at_cap_accepted(self, db: Path, data_dir: Path) -> None:
        """Exactamente _MAX_DOCS_PER_INTAKE docs → todos escritos, ninguno omitido."""
        docs = [{"name": f"doc{i}.txt", "content": b"x"} for i in range(_MAX_DOCS_PER_INTAKE)]
        _, skipped = _create(db, data_dir, docs=docs)
        intake_dirs = list((data_dir / "intake").iterdir())
        written = list((intake_dirs[0] / "docs").iterdir())
        assert len(written) == _MAX_DOCS_PER_INTAKE
        assert skipped == []

    def test_skipped_names_are_the_last_ones(self, db: Path, data_dir: Path) -> None:
        """Los primeros _MAX_DOCS_PER_INTAKE se guardan; los últimos N van a skipped."""
        total = _MAX_DOCS_PER_INTAKE + 3
        docs = [{"name": f"doc{i:02d}.txt", "content": b"x"} for i in range(total)]
        _, skipped = _create(db, data_dir, docs=docs)
        # Los últimos 3 son doc22, doc23, doc24 (si MAX=25)
        expected_skipped = [f"doc{i:02d}.txt" for i in range(_MAX_DOCS_PER_INTAKE, total)]
        assert skipped[:3] == expected_skipped  # primeros 3 del skipped son los excess


# ---------------------------------------------------------------------------
# M1 — feedback de docs omitidos en skipped_docs
# ---------------------------------------------------------------------------

class TestSkippedDocsFeedback:
    def test_invalid_ext_reported_in_skipped(self, db: Path, data_dir: Path) -> None:
        _, skipped = _create(db, data_dir, docs=[
            {"name": "bad.exe", "content": b"MZ"},
            {"name": "good.md", "content": b"# ok"},
        ])
        assert "bad.exe" in skipped
        assert "good.md" not in skipped

    def test_oversized_reported_in_skipped(self, db: Path, data_dir: Path) -> None:
        oversized = b"x" * (_MAX_DOC_BYTES + 1)
        _, skipped = _create(db, data_dir, docs=[{"name": "big.txt", "content": oversized}])
        assert "big.txt" in skipped

    def test_hidden_file_reported_in_skipped(self, db: Path, data_dir: Path) -> None:
        _, skipped = _create(db, data_dir, docs=[{"name": ".hidden.txt", "content": b"s"}])
        assert ".hidden.txt" in skipped

    def test_no_skipped_when_all_valid(self, db: Path, data_dir: Path) -> None:
        _, skipped = _create(db, data_dir, docs=[{"name": "spec.md", "content": b"ok"}])
        assert skipped == []


# ---------------------------------------------------------------------------
# Whitelist de extensión
# ---------------------------------------------------------------------------

class TestDocWhitelist:
    def test_rejects_exe(self, db: Path, data_dir: Path) -> None:
        _create(db, data_dir, docs=[{"name": "malware.exe", "content": b"MZ\x90\x00"}])
        intake_dirs = list((data_dir / "intake").iterdir())
        docs_dir = intake_dirs[0] / "docs"
        assert not (docs_dir / "malware.exe").exists()

    def test_rejects_bin(self, db: Path, data_dir: Path) -> None:
        _create(db, data_dir, docs=[{"name": "image.png", "content": b"\x89PNG"}])
        intake_dirs = list((data_dir / "intake").iterdir())
        docs_dir = intake_dirs[0] / "docs"
        assert not list(docs_dir.iterdir()), "no se deben haber escrito archivos no permitidos"

    def test_accepts_allowed_extensions(self, db: Path, data_dir: Path) -> None:
        """Verifica que todas las extensiones de la whitelist se aceptan."""
        docs = [{"name": f"file{ext}", "content": b"content"} for ext in _ALLOWED_DOC_EXTS]
        _create(db, data_dir, docs=docs)
        intake_dirs = list((data_dir / "intake").iterdir())
        docs_dir = intake_dirs[0] / "docs"
        written = {f.name for f in docs_dir.iterdir()}
        expected = {f"file{ext}" for ext in _ALLOWED_DOC_EXTS}
        assert written == expected

    def test_mixed_valid_and_invalid(self, db: Path, data_dir: Path) -> None:
        docs = [
            {"name": "good.md", "content": b"# ok"},
            {"name": "bad.exe", "content": b"MZ"},
        ]
        _create(db, data_dir, docs=docs)
        intake_dirs = list((data_dir / "intake").iterdir())
        docs_dir = intake_dirs[0] / "docs"
        assert (docs_dir / "good.md").exists()
        assert not (docs_dir / "bad.exe").exists()


# ---------------------------------------------------------------------------
# Cap de tamaño
# ---------------------------------------------------------------------------

class TestDocSizeCap:
    def test_rejects_oversized_doc(self, db: Path, data_dir: Path) -> None:
        oversized = b"x" * (_MAX_DOC_BYTES + 1)
        _create(db, data_dir, docs=[{"name": "big.txt", "content": oversized}])
        intake_dirs = list((data_dir / "intake").iterdir())
        docs_dir = intake_dirs[0] / "docs"
        assert not (docs_dir / "big.txt").exists()

    def test_accepts_doc_at_exact_cap(self, db: Path, data_dir: Path) -> None:
        exact = b"x" * _MAX_DOC_BYTES
        _create(db, data_dir, docs=[{"name": "exact.txt", "content": exact}])
        intake_dirs = list((data_dir / "intake").iterdir())
        assert (intake_dirs[0] / "docs" / "exact.txt").exists()


# ---------------------------------------------------------------------------
# Nombres de doc inválidos (path-traversal, vacío, oculto)
# ---------------------------------------------------------------------------

class TestDocNameSanitization:
    def test_path_traversal_blocked(self, db: Path, data_dir: Path) -> None:
        _create(db, data_dir, docs=[{"name": "../../etc/passwd", "content": b"root:x"}])
        assert not (data_dir / "etc").exists()

    def test_hidden_file_blocked(self, db: Path, data_dir: Path) -> None:
        _create(db, data_dir, docs=[{"name": ".hidden.txt", "content": b"secret"}])
        intake_dirs = list((data_dir / "intake").iterdir())
        docs_dir = intake_dirs[0] / "docs"
        assert not (docs_dir / ".hidden.txt").exists()

    def test_empty_name_skipped(self, db: Path, data_dir: Path) -> None:
        row_id, _ = _create(db, data_dir, docs=[{"name": "", "content": b"data"}])
        assert row_id >= 1  # intake creado ok


# ---------------------------------------------------------------------------
# Validación de brief
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_empty_brief_raises(self, db: Path, data_dir: Path) -> None:
        with pytest.raises(ValueError, match="brief_text"):
            _create(db, data_dir, brief="   ")


# ---------------------------------------------------------------------------
# list_intakes / get_intake / set_status
# ---------------------------------------------------------------------------

class TestCRUD:
    def test_list_intakes_empty(self, db: Path) -> None:
        ensure_intake_table(db)
        assert list_intakes(db) == []

    def test_list_intakes_returns_all(self, db: Path, data_dir: Path) -> None:
        _create(db, data_dir, client="c1", brief="brief1")
        _create(db, data_dir, client="c2", brief="brief2")
        items = list_intakes(db)
        assert len(items) == 2

    def test_list_intakes_filter_by_status(self, db: Path, data_dir: Path) -> None:
        row_id, _ = _create(db, data_dir, client="c1", brief="brief1")
        _create(db, data_dir, client="c2", brief="brief2")
        set_status(db, row_id, "done")
        pending = list_intakes(db, status="pending")
        done = list_intakes(db, status="done")
        assert len(pending) == 1
        assert len(done) == 1
        assert done[0]["client_id"] == "c1"

    def test_get_intake_not_found(self, db: Path) -> None:
        ensure_intake_table(db)
        assert get_intake(db, 9999) is None

    def test_get_intake_found(self, db: Path, data_dir: Path) -> None:
        row_id, _ = _create(db, data_dir, client="c1", brief="brief texto")
        item = get_intake(db, row_id)
        assert item is not None
        assert item["id"] == row_id
        assert item["client_id"] == "c1"

    def test_set_status_valid(self, db: Path, data_dir: Path) -> None:
        row_id, _ = _create(db, data_dir, client="c1")
        set_status(db, row_id, "in_progress")
        item = get_intake(db, row_id)
        assert item is not None
        assert item["status"] == "in_progress"

    def test_set_status_all_valid_statuses(self, db: Path, data_dir: Path) -> None:
        for status in _VALID_STATUSES:
            _ci_mod._INTAKE_TABLE_READY = False
            row_id, _ = _create(db, data_dir, client="c1")
            set_status(db, row_id, status)
            item = get_intake(db, row_id)
            assert item is not None
            assert item["status"] == status

    def test_set_status_invalid_raises(self, db: Path, data_dir: Path) -> None:
        row_id, _ = _create(db, data_dir, client="c1")
        with pytest.raises(ValueError, match="inválido"):
            set_status(db, row_id, "unknown_status")


# ---------------------------------------------------------------------------
# Aislamiento por client_id
# ---------------------------------------------------------------------------

class TestClientIsolation:
    def test_intakes_are_scoped(self, db: Path, data_dir: Path) -> None:
        id1, _ = _create(db, data_dir, client="clienta", brief="brief A")
        id2, _ = _create(db, data_dir, client="clientb", brief="brief B")
        item1 = get_intake(db, id1)
        item2 = get_intake(db, id2)
        assert item1 is not None and item1["client_id"] == "clienta"
        assert item2 is not None and item2["client_id"] == "clientb"
        assert id1 != id2

    def test_intake_dirs_are_separate(self, db: Path, data_dir: Path) -> None:
        _create(db, data_dir, client="clienta", docs=[{"name": "spec.txt", "content": b"A"}])
        _create(db, data_dir, client="clientb", docs=[{"name": "spec.txt", "content": b"B"}])
        intake_dirs = sorted((data_dir / "intake").iterdir())
        assert len(intake_dirs) == 2
        content_a = (intake_dirs[0] / "docs" / "spec.txt").read_bytes()
        content_b = (intake_dirs[1] / "docs" / "spec.txt").read_bytes()
        assert content_a != content_b


# ---------------------------------------------------------------------------
# Edge cases con DB inexistente
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_list_intakes_nonexistent_db(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.db"
        assert list_intakes(missing) == []

    def test_get_intake_nonexistent_db(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent.db"
        assert get_intake(missing, 1) is None
