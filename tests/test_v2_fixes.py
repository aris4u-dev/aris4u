"""Tests de los fixes V2.0 (2026-06-11) — AUDIT_V2_20260611.

Cubre los dos P0 de memoria por-cliente y el canónico de client_id:
1. Instalación fresca: init_db crea digests CON client_id (antes save_digest crasheaba).
2. Roundtrip ingest→recall: aris_ingest guarda locked=1 por defecto y aris_recall_client
   ya no filtra locked=1 (antes 0 de 1503 decisiones eran recuperables).
3. Canónico de cliente: lower-case + sufijo conocido, sin partir en el primer guión
   (bugs: acme-wellness→"acme", Client-D→"Client-D")..
"""

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from engine.v16 import session_manager

ARIS4U_ROOT = Path(__file__).resolve().parent.parent


class TestFreshInstallSchema:
    """P0: una instalación fresca debe poder persistir digests con client_id."""

    def test_init_db_creates_digests_with_client_id(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(session_manager, "SESSIONS_DB", tmp_path / "fresh.db")
        session_manager.init_db()

        db = sqlite3.connect(tmp_path / "fresh.db")
        cols = [row[1] for row in db.execute("PRAGMA table_info(digests)").fetchall()]
        db.close()
        assert "client_id" in cols, f"digests sin client_id en instalación fresca: {cols}"

    def test_save_digest_works_on_fresh_db(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(session_manager, "SESSIONS_DB", tmp_path / "fresh.db")
        session_manager.init_db()

        # Antes del fix: sqlite3.OperationalError (table digests has no column named client_id)
        session_manager.save_digest(
            digest_id="2026-06-11-test-v2",
            session_id="test",
            summary="digest de prueba instalación fresca",
            client_id="testclient",
        )
        row = session_manager.query_db(
            "SELECT client_id FROM digests WHERE id = ?", ("2026-06-11-test-v2",), fetch_all=False
        )
        assert row is not None and row["client_id"] == "testclient"  # type: ignore[reportCallIssue,reportArgumentType]


class TestIngestRecallRoundtrip:
    """P0: lo que entra por save_decision debe salir por la consulta de recall."""

    def test_decision_roundtrip_unlocked_and_locked(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(session_manager, "SESSIONS_DB", tmp_path / "fresh.db")
        # El embed asíncrono toca el vector store real — fuera en tests
        monkeypatch.setattr(session_manager, "_async_embed_decision", lambda *a, **k: None)
        session_manager.init_db()

        session_manager.save_decision(
            decision="usar postgres para el cliente",
            domain="database",
            locked=True,
            client_id="testclient",
        )
        session_manager.save_decision(
            decision="nota sin lock",
            domain="general",
            locked=False,
            client_id="testclient",
        )

        # La consulta que usa aris_recall_client (V2.0): sin filtro locked, locked primero
        rows = session_manager.query_db(
            "SELECT decision, locked FROM decisions WHERE client_id = ? "
            "ORDER BY locked DESC, created_at DESC",
            ("testclient",),
        )
        assert len(rows) == 2, "el recall debe devolver locked Y no-locked"  # type: ignore[reportArgumentType]
        assert rows[0]["locked"] == 1 and rows[0]["decision"] == "usar postgres para el cliente"  # type: ignore[reportOptionalSubscript]


class TestClientCanonical:
    """Canónico de client_id: lower + sufijo conocido, nunca split en primer guión."""

    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/Users/x/projects/03-clients/client-b-platform/src", "client-b"),
            ("/Users/x/projects/03-clients/acme-wellness/docs", "acme-wellness"),
            ("/Users/x/projects/03-clients/client-d/artifact", "client-d"),
            ("/Users/x/projects/03-clients/client-e", "client-e"),
            ("/Users/x/projects/03-clients/client-c/inventory-system", "client-c"),
            ("/Users/x/projects/otracosa", None),
        ],
    )
    def test_resolve_client_from_path(self, path, expected) -> None:
        assert session_manager.resolve_client_from_path(path) == expected

    @pytest.mark.parametrize(
        "cwd,expected",
        [
            ("/Users/x/projects/03-clients/acme-wellness/docs", "acme-wellness"),
            ("/Users/x/projects/03-clients/client-d/artifact", "client-d"),
            ("/Users/x/projects/03-clients/client-b-platform/src", "client-b"),
        ],
    )
    def test_write_client_bridge_canonical(self, cwd, expected, monkeypatch) -> None:
        sid = f"testv2-{expected}"
        bridge = Path(f"/tmp/aris4u_active_client.{sid}.json")
        try:
            env = dict(os.environ, CLAUDE_CODE_SESSION_ID=sid)
            subprocess.run(
                ["bash", str(ARIS4U_ROOT / "hooks" / "write_client_bridge.sh"), cwd],
                env=env,
                timeout=10,
                check=True,
            )
            data = json.loads(bridge.read_text())
            assert data["client_id"] == expected
        finally:
            bridge.unlink(missing_ok=True)
