"""Tests del auto-verificador (selfcheck): las partes puras, sin necesidad de server vivo."""
from __future__ import annotations

from pathlib import Path

from aris4u_console import selfcheck


def test_check_no_orphan_detects_present(tmp_path: Path) -> None:
    """check_no_orphan falla si existe console/data/sessions.db."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "sessions.db").write_bytes(b"x")
    c = selfcheck.check_no_orphan(tmp_path)
    assert c.ok is False
    assert "EXISTE" in c.detail


def test_check_no_orphan_passes_when_absent(tmp_path: Path) -> None:
    """check_no_orphan pasa si no hay DB huérfana."""
    c = selfcheck.check_no_orphan(tmp_path)
    assert c.ok is True


def test_endpoints_list_nonempty() -> None:
    """El contrato de endpoints a verificar no está vacío (cubre las secciones vivas)."""
    assert len(selfcheck._ENDPOINTS) >= 15
    assert "/memory" in selfcheck._ENDPOINTS
    assert "/config" in selfcheck._ENDPOINTS


def test_scalar_failsoft_on_missing_db(tmp_path: Path) -> None:
    """_scalar devuelve -1 (no crashea) si la DB no existe."""
    assert selfcheck._scalar(tmp_path / "nope.db", "SELECT COUNT(*) FROM x") == -1
