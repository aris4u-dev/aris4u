"""Tests del tagger de client_id para observations (tools/tag_observations_client.py).

Cubre:
  - canonicalize_client: sufijos, alias, casos borde (acme-wellness no → 'acme')
  - infer_client_from_paths: paths de cada cliente, colisión multi-cliente,
    prioridad no-aris4u en colisión, sin señal → None
  - infer_client: prioridad 1 paths vs prioridad 2 project
  - idempotencia: correr el tagger 2x sobre una DB temporal no cambia nada la 2a vez
  - JSON malformado en files_* no aborta el barrido

``tools/`` no es paquete → se añade a sys.path como hacen los tests hermanos.

NUNCA se usa la DB real (~/.claude-mem/claude-mem.db).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Import del módulo bajo test (mismo patrón que test_recall_usefulness.py)
# ---------------------------------------------------------------------------
_TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import tag_observations_client as toc  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: DB temporal in-memory / en /tmp (NUNCA la real)
# ---------------------------------------------------------------------------

def _make_db(path: str = ":memory:") -> sqlite3.Connection:
    """Crea una DB SQLite temporal con el schema mínimo de observations."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id               INTEGER PRIMARY KEY,
            memory_session_id TEXT NOT NULL DEFAULT '',
            project          TEXT NOT NULL DEFAULT '',
            text             TEXT,
            type             TEXT NOT NULL DEFAULT 'test',
            files_read       TEXT,
            files_modified   TEXT,
            created_at       TEXT NOT NULL DEFAULT '',
            created_at_epoch INTEGER NOT NULL DEFAULT 0,
            client_id        TEXT
        )
    """)
    conn.commit()
    return conn


def _insert_obs(
    conn: sqlite3.Connection,
    *,
    project: str = "YOUR_USERNAME",
    files_read: Optional[list[str]] = None,
    files_modified: Optional[list[str]] = None,
    client_id: Optional[str] = None,
    epoch: int = 0,
) -> int:
    """Inserta una observation de prueba y devuelve su id."""
    cur = conn.execute(
        """
        INSERT INTO observations
            (memory_session_id, project, type, files_read, files_modified,
             created_at, created_at_epoch, client_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ses-test",
            project,
            "test",
            json.dumps(files_read) if files_read is not None else None,
            json.dumps(files_modified) if files_modified is not None else None,
            "2026-01-01",
            epoch or int(time.time()),
            client_id,
        ),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


# ---------------------------------------------------------------------------
# canonicalize_client
# ---------------------------------------------------------------------------

class TestCanonicalizeClient:
    """Espejo de la lógica de write_client_bridge.sh."""

    def test_client_b_platform_strips_suffix(self) -> None:
        assert toc.canonicalize_client("client-b-platform") == "client-b"

    def test_lab1_app_strips_suffix(self) -> None:
        assert toc.canonicalize_client("lab-project-1-app") == "lab-project-1"

    def test_lab1_web_strips_suffix(self) -> None:
        # If a lab-project-1-web dir existed it should map to lab-project-1
        assert toc.canonicalize_client("lab-project-1-web") == "lab-project-1"

    def test_acme_wellness_not_broken(self) -> None:
        """NUNCA %%-* greedy: 'acme-wellness' no debe → 'acme'."""
        # No está en el alias map → None; lo importante es que no → 'acme'
        result = toc.canonicalize_client("acme-wellness")
        assert result != "acme"

    def test_lab2_alias(self) -> None:
        assert toc.canonicalize_client("lab-project-2") == "client-c"

    def test_inventory_system_alias(self) -> None:
        assert toc.canonicalize_client("client-c-inventory") == "client-c"

    def test_old_lab1_alias(self) -> None:
        assert toc.canonicalize_client("old-name-lab-1") == "lab-project-1"

    def test_client_d_co_alias(self) -> None:
        assert toc.canonicalize_client("client-d-co") == "client-d"

    def test_artifact_alias(self) -> None:
        assert toc.canonicalize_client("client-d-artifact") == "client-d"

    def test_client_a_passthrough(self) -> None:
        assert toc.canonicalize_client("client-a") == "client-a"

    def test_aris4u_passthrough(self) -> None:
        assert toc.canonicalize_client("aris4u") == "aris4u"

    def test_case_insensitive(self) -> None:
        assert toc.canonicalize_client("CLIENT-B-PLATFORM") == "client-b"

    def test_unknown_returns_none(self) -> None:
        assert toc.canonicalize_client("some-random-project") is None

    def test_empty_returns_none(self) -> None:
        assert toc.canonicalize_client("") is None

    def test_lab_project_4_passthrough(self) -> None:
        assert toc.canonicalize_client("lab-project-4") == "lab-project-4"

    def test_lab_project_5_passthrough(self) -> None:
        assert toc.canonicalize_client("lab-project-5") == "lab-project-5"


# ---------------------------------------------------------------------------
# infer_client_from_paths — señal desde files_read / files_modified
# ---------------------------------------------------------------------------

class TestInferClientFromPaths:
    """Verifica inferencia de client_id desde arrays JSON de paths."""

    # --- Cada cliente tiene sus paths representativos ---

    def test_client_b_via_path(self) -> None:
        paths = json.dumps(["${HOME}/projects/client-b-platform/mod-crm/foo.py"])
        assert toc.infer_client_from_paths(paths, None) == "client-b"

    def test_client_b_via_broad_scan(self) -> None:
        paths = json.dumps(["${HOME}/projects/client-b-platform/services/api.py"])
        assert toc.infer_client_from_paths(paths, None) == "client-b"

    def test_client_c_via_lab2_alias(self) -> None:
        paths = json.dumps(["${HOME}/projects/lab-project-2/src/crm.py"])
        assert toc.infer_client_from_paths(paths, None) == "client-c"

    def test_client_c_via_inventory(self) -> None:
        paths = json.dumps(["${HOME}/projects/client-c-inventory/migrations/001.sql"])
        assert toc.infer_client_from_paths(paths, None) == "client-c"

    def test_lab1_via_path(self) -> None:
        paths = json.dumps(["${HOME}/projects/lab-project-1/lib/main.dart"])
        assert toc.infer_client_from_paths(paths, None) == "lab-project-1"

    def test_lab1_via_old_alias(self) -> None:
        paths = json.dumps(["${HOME}/projects/lab-project-1/scenes/scene1.py"])
        assert toc.infer_client_from_paths(paths, None) == "lab-project-1"

    def test_client_a_via_path(self) -> None:
        paths = json.dumps(["${HOME}/projects/client-a/src/screens/call.tsx"])
        assert toc.infer_client_from_paths(paths, None) == "client-a"

    def test_client_d_via_co(self) -> None:
        paths = json.dumps(["${HOME}/projects/client-d-co/backend/api.py"])
        assert toc.infer_client_from_paths(paths, None) == "client-d"

    def test_client_d_via_artifact(self) -> None:
        paths = json.dumps(["${HOME}/projects/client-d-artifact/compliance/aml.py"])
        assert toc.infer_client_from_paths(paths, None) == "client-d"

    def test_aris4u_via_path(self) -> None:
        paths = json.dumps(["/Users/YOUR_USERNAME/projects/aris4u/hooks/dispatch.py"])
        assert toc.infer_client_from_paths(paths, None) == "aris4u"

    def test_lab_project_4_via_known_top(self) -> None:
        paths = json.dumps(["${HOME}/projects/lab-project-4/backend/api.py"])
        assert toc.infer_client_from_paths(paths, None) == "lab-project-4"

    def test_lab_project_5_via_known_top(self) -> None:
        paths = json.dumps(["${HOME}/projects/lab-project-5/predict.py"])
        assert toc.infer_client_from_paths(paths, None) == "lab-project-5"

    # --- files_modified como fuente ---

    def test_infer_from_files_modified(self) -> None:
        modified = json.dumps(["${HOME}/projects/client-a/src/app.tsx"])
        assert toc.infer_client_from_paths(None, modified) == "client-a"

    def test_both_fields_same_client(self) -> None:
        read = json.dumps(["${HOME}/projects/lab-project-1/pubspec.yaml"])
        modified = json.dumps(["${HOME}/projects/lab-project-1/lib/main.dart"])
        assert toc.infer_client_from_paths(read, modified) == "lab-project-1"

    # --- Colisión multi-cliente ---

    def test_collision_prefers_non_aris4u(self) -> None:
        """Paths touching aris4u AND client-b → prefers client-b."""
        read = json.dumps([
            "/Users/YOUR_USERNAME/projects/aris4u/hooks/dispatch.py",
            "${HOME}/projects/client-b-platform/api.py",
        ])
        assert toc.infer_client_from_paths(read, None) == "client-b"

    def test_collision_two_non_aris4u_returns_none(self) -> None:
        """Paths touching client-b AND lab-project-1 → ambiguous → None."""
        read = json.dumps([
            "${HOME}/projects/client-b-platform/api.py",
            "${HOME}/projects/lab-project-1/lib/main.dart",
        ])
        assert toc.infer_client_from_paths(read, None) is None

    def test_only_aris4u_paths_returns_aris4u(self) -> None:
        read = json.dumps([
            "/Users/YOUR_USERNAME/projects/aris4u/engine/v16/pipeline.py",
            "/Users/YOUR_USERNAME/projects/aris4u/hooks/dispatch.py",
        ])
        assert toc.infer_client_from_paths(read, None) == "aris4u"

    # --- Sin señal ---

    def test_no_paths_returns_none(self) -> None:
        assert toc.infer_client_from_paths(None, None) is None

    def test_empty_arrays_return_none(self) -> None:
        assert toc.infer_client_from_paths("[]", "[]") is None

    def test_unrelated_paths_return_none(self) -> None:
        paths = json.dumps(["/tmp/extract_v2.py", "/usr/local/bin/python3"])
        assert toc.infer_client_from_paths(paths, None) is None

    def test_malformed_json_returns_none(self) -> None:
        """JSON malformado no aborta — devuelve None silenciosamente."""
        assert toc.infer_client_from_paths("{not: json}", None) is None

    def test_null_string_returns_none(self) -> None:
        assert toc.infer_client_from_paths("null", "null") is None

    # --- Paths históricos del sistema real ---

    def test_media_path_client_b(self) -> None:
        """Paths on external disk are detected too."""
        paths = json.dumps(["/media/YOUR_USERNAME/CLAUDE/client-b/config.json"])
        assert toc.infer_client_from_paths(paths, None) == "client-b"

    def test_lab1_dart_files(self) -> None:
        read = json.dumps(["lib/services/ride_service.dart", "lib/models/ride.dart"])
        # Sin prefijo /projects/lab-project-1/ → no hay señal (rutas relativas sin contexto)
        assert toc.infer_client_from_paths(read, None) is None


# ---------------------------------------------------------------------------
# infer_client — prioridad 1 (paths) vs prioridad 2 (project)
# ---------------------------------------------------------------------------

class TestInferClient:
    """Verifica la lógica de prioridad en infer_client."""

    def test_paths_win_over_project(self) -> None:
        """If files_read points to lab-project-1 but project=aris4u, lab-project-1 wins."""
        row = {
            "files_read": json.dumps(["${HOME}/projects/lab-project-1/lib/main.dart"]),
            "files_modified": None,
            "project": "aris4u",
        }
        assert toc.infer_client(row) == "lab-project-1"

    def test_project_fallback_when_no_paths(self) -> None:
        row = {
            "files_read": None,
            "files_modified": "[]",
            "project": "lab-project-1-app",
        }
        assert toc.infer_client(row) == "lab-project-1"

    def test_generic_project_skipped(self) -> None:
        row = {
            "files_read": None,
            "files_modified": None,
            "project": "YOUR_USERNAME",
        }
        assert toc.infer_client(row) is None

    def test_observer_sessions_skipped(self) -> None:
        row = {
            "files_read": None,
            "files_modified": None,
            "project": "observer-sessions",
        }
        assert toc.infer_client(row) is None

    def test_aris4u_project_fallback(self) -> None:
        row = {"files_read": None, "files_modified": None, "project": "aris4u"}
        assert toc.infer_client(row) == "aris4u"

    def test_no_signal_returns_none(self) -> None:
        row = {
            "files_read": json.dumps(["/tmp/foo.py"]),
            "files_modified": None,
            "project": "YOUR_USERNAME",
        }
        assert toc.infer_client(row) is None


# ---------------------------------------------------------------------------
# Idempotencia: correr el tagger 2x no cambia nada la 2a vez
# ---------------------------------------------------------------------------

class TestIdempotence:
    """El tagger no debe sobrescribir client_id ya asignado ni duplicar trabajo."""

    def _run_tagger(self, db_path: str) -> None:
        """Corre el tagger CLI sobre la DB indicada (--since 0 para todas las filas)."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(_TOOLS_DIR / "tag_observations_client.py"),
             "--db", db_path, "--since", "0"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Tagger falló: {result.stderr}"

    def test_idempotent_on_tmp_db(self, tmp_path: Path) -> None:
        """Correr el tagger dos veces produce el mismo resultado."""
        db_file = str(tmp_path / "test_idem.db")
        conn = _make_db(db_file)

        # Insertar observations con señal clara
        _insert_obs(conn, files_read=["${HOME}/projects/lab-project-1/lib/main.dart"])
        _insert_obs(conn, files_read=["/Users/YOUR_USERNAME/projects/aris4u/hooks/dispatch.py"])
        _insert_obs(conn, project="aris4u")
        _insert_obs(conn, project="YOUR_USERNAME")  # genérico → NULL
        conn.close()

        # Primera ejecución
        self._run_tagger(db_file)

        conn2 = sqlite3.connect(db_file)
        snap1 = {r[0]: r[1] for r in conn2.execute("SELECT id, client_id FROM observations")}
        conn2.close()

        # Segunda ejecución
        self._run_tagger(db_file)

        conn3 = sqlite3.connect(db_file)
        snap2 = {r[0]: r[1] for r in conn3.execute("SELECT id, client_id FROM observations")}
        conn3.close()

        assert snap1 == snap2, "La 2a ejecución cambió client_ids ya asignados"

    def test_respects_existing_client_id(self, tmp_path: Path) -> None:
        """Una fila con client_id ya asignado NO se toca."""
        db_file = str(tmp_path / "test_existing.db")
        conn = _make_db(db_file)

        # Observation ya etiquetada manualmente
        obs_id = _insert_obs(
            conn,
            files_read=["${HOME}/projects/lab-project-1/lib/main.dart"],
            client_id="client-a",  # manual label different from what paths would infer
        )
        conn.close()

        self._run_tagger(db_file)

        conn2 = sqlite3.connect(db_file)
        row = conn2.execute(
            "SELECT client_id FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        conn2.close()

        # Tagger must NOT have overwritten the manual label "client-a"
        assert row[0] == "client-a", "El tagger pisó un client_id ya asignado"

    def test_null_stays_null_when_no_signal(self, tmp_path: Path) -> None:
        """Observations sin señal quedan con client_id NULL tras ambas ejecuciones."""
        db_file = str(tmp_path / "test_null.db")
        conn = _make_db(db_file)
        obs_id = _insert_obs(conn, project="YOUR_USERNAME", files_read=["/tmp/foo.py"])
        conn.close()

        self._run_tagger(db_file)
        self._run_tagger(db_file)

        conn2 = sqlite3.connect(db_file)
        row = conn2.execute(
            "SELECT client_id FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        conn2.close()

        assert row[0] is None


# ---------------------------------------------------------------------------
# _extract_paths — tolerancia a datos malformados
# ---------------------------------------------------------------------------

class TestExtractPaths:
    """Verifica tolerancia de _extract_paths a datos sucios."""

    def test_valid_json_array(self) -> None:
        assert toc._extract_paths('["a.py", "b.py"]') == ["a.py", "b.py"]

    def test_empty_array(self) -> None:
        assert toc._extract_paths("[]") == []

    def test_null_string(self) -> None:
        assert toc._extract_paths("null") == []

    def test_none_input(self) -> None:
        assert toc._extract_paths(None) == []

    def test_malformed_json(self) -> None:
        assert toc._extract_paths("{not: valid}") == []

    def test_empty_string(self) -> None:
        assert toc._extract_paths("") == []

    def test_filters_empty_elements(self) -> None:
        result = toc._extract_paths('["a.py", "", null, "b.py"]')
        assert "" not in result
        assert "a.py" in result and "b.py" in result
