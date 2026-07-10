"""Intake de proyectos para ARIS4U — superficie de captura no-técnica.

Un CEO/fundador describe lo que quiere (brief) + sube docs de soporte; esto
dispara (en builds futuros) el pipeline de construcción.  El intake persiste en
``sessions.db`` (tabla ``intake_requests``) y los archivos en
``data/intake/<intake_id>/``.

Restricciones de diseño:
- Cero dependencias externas (solo stdlib + sqlite3).
- Aislamiento por ``client_id``: ningún cliente puede ver el intake de otro.
- Whitelist de extensión + cap de tamaño por archivo + cap de número de docs.
- client_id validado contra ``^[a-z0-9_-]+$`` (anti log-injection).
- El ingest ARIS4U se llama programáticamente (no via MCP); si falla, fail-open
  (el intake se crea igual y el fallo queda logueado).
"""
from __future__ import annotations

import logging
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

# Extensiones permitidas para documentos adjuntos
_ALLOWED_DOC_EXTS: frozenset[str] = frozenset({
    ".txt", ".md", ".pdf", ".csv", ".json", ".yaml", ".yml",
    ".toml", ".rst", ".html", ".htm",
})

# Tamaño máximo por archivo adjunto (2 MB)
_MAX_DOC_BYTES: int = 2 * 1024 * 1024

# Número máximo de docs por intake (H1 — anti agotamiento de disco)
_MAX_DOCS_PER_INTAKE: int = 25

# Patrón de client_id válido (M2 — anti log-injection)
_CLIENT_ID_RE: re.Pattern[str] = re.compile(r"^[a-z0-9_-]+$")

# Estado inicial de un intake recién creado
_STATUS_PENDING = "pending"

_VALID_STATUSES: frozenset[str] = frozenset({
    "pending", "in_progress", "building", "done", "failed", "rejected",
    "needs_review",  # B1-bis: build exited 0 but produced no new commits
})

# L1 — once-per-process flag (igual que _COWORK_TABLE_READY en mcp_server.py)
_INTAKE_TABLE_READY: bool = False


# ---------------------------------------------------------------------------
# Migración idempotente (patrón ensure_comments_table de project_timeline.py)
# ---------------------------------------------------------------------------

def ensure_intake_table(db_path: str | Path) -> None:
    """Crea la tabla ``intake_requests`` si no existe (idempotente, once-per-process).

    Sigue el patrón de ``_COWORK_TABLE_READY`` en mcp_server.py: el flag de módulo
    ``_INTAKE_TABLE_READY`` evita el CREATE TABLE IF NOT EXISTS + commit en cada
    ``create_intake`` dentro del mismo proceso (L1).

    Args:
        db_path: Ruta al archivo SQLite (sessions.db).
    """
    global _INTAKE_TABLE_READY
    if _INTAKE_TABLE_READY:
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intake_requests (
                id         INTEGER PRIMARY KEY,
                client_id  TEXT    NOT NULL,
                brief_path TEXT    NOT NULL,
                docs_dir   TEXT    NOT NULL,
                status     TEXT    NOT NULL DEFAULT 'pending',
                created_at TEXT    NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    _INTAKE_TABLE_READY = True


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def create_intake(
    db_path: str | Path,
    client_id: str,
    brief_text: str,
    doc_files: list[dict[str, object]],  # [{name: str, content: bytes}]
    *,
    data_dir: Path | None = None,
) -> tuple[int, list[str]]:
    """Crea un nuevo intake: persiste brief + docs + fila en DB.

    Genera un ``intake_id`` con ``secrets.token_hex(12)`` (sin math.random /
    uuid basado en tiempo).  El brief se escribe en
    ``data/intake/<intake_id>/brief.md`` y cada documento adjunto (validado
    contra whitelist) en ``data/intake/<intake_id>/docs/``.

    El ingest ARIS4U (para que la memoria lo recuerde por cliente) se intenta
    programáticamente; si falla, el intake se crea igual y el error queda en el
    log (fail-open).

    Args:
        db_path: Ruta a sessions.db.
        client_id: Identificador del cliente.  Debe coincidir con
            ``^[a-z0-9_-]+$`` (tras strip + lower).  Si no matchea se lanza
            ``ValueError`` (M2 — anti log-injection).
        brief_text: Descripción libre del proyecto (no vacía).
        doc_files: Lista de dicts ``{name: str, content: bytes}``.  Se aceptan
            como máximo ``_MAX_DOCS_PER_INTAKE`` (25) docs; los que superan el
            cap o no pasan la whitelist se omiten y se devuelven en
            ``skipped_docs`` (M1, H1).
        data_dir: Directorio raíz de datos.  Por defecto se infiere desde
            ``db_path`` (``db_path.parent``).

    Returns:
        Tupla ``(row_id, skipped_docs)`` donde ``row_id`` es el PK del nuevo
        intake en ``intake_requests`` y ``skipped_docs`` es la lista de nombres
        de archivo que se descartaron (para informar al caller/UI).

    Raises:
        ValueError: Si ``client_id`` vacío, formato inválido, o ``brief_text``
            vacío.
        sqlite3.Error: Si la inserción en la DB falla.
    """
    client_id = client_id.strip().lower()
    brief_text = brief_text.strip()
    if not client_id:
        raise ValueError("client_id no puede estar vacío")
    if not _CLIENT_ID_RE.match(client_id):
        raise ValueError(
            f"client_id inválido '{client_id}': solo se permiten letras minúsculas, "
            "dígitos, guion y guion_bajo (^[a-z0-9_-]+$)"
        )
    if not brief_text:
        raise ValueError("brief_text no puede estar vacío")

    db_path = Path(db_path)
    if data_dir is None:
        data_dir = db_path.parent

    # Generar ID único con entropía criptográfica (24 hex chars = 96 bits)
    intake_id = secrets.token_hex(12)
    intake_dir = data_dir / "intake" / intake_id
    brief_path = intake_dir / "brief.md"
    docs_dir = intake_dir / "docs"

    # Escribir archivos al disco ANTES de insertar en DB (fail-fast si el FS falla)
    intake_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    brief_path.write_text(brief_text, encoding="utf-8")

    skipped_docs: list[str] = []

    # H1 — cap de número de docs (primeros _MAX_DOCS_PER_INTAKE; el resto se descarta)
    if len(doc_files) > _MAX_DOCS_PER_INTAKE:
        discarded = doc_files[_MAX_DOCS_PER_INTAKE:]
        doc_files = doc_files[:_MAX_DOCS_PER_INTAKE]
        discarded_names = [
            str(d.get("name") or "").strip() or "<sin nombre>"
            for d in discarded
        ]
        _log.warning(
            "intake %s: %d docs superan el cap de %d; descartados: %s",
            intake_id, len(discarded), _MAX_DOCS_PER_INTAKE, discarded_names,
        )
        skipped_docs.extend(discarded_names)

    for doc in doc_files:
        raw_name = doc.get("name")
        name: str = (str(raw_name) if raw_name is not None else "").strip()
        raw_content = doc.get("content")
        content: bytes = raw_content if isinstance(raw_content, bytes) else b""
        if not name:
            _log.warning("intake %s: doc sin nombre, omitido", intake_id)
            skipped_docs.append("<sin nombre>")
            continue
        ext = Path(name).suffix.lower()
        if ext not in _ALLOWED_DOC_EXTS:
            _log.warning(
                "intake %s: extension '%s' no permitida, doc '%s' omitido",
                intake_id, ext, name,
            )
            skipped_docs.append(name)
            continue
        if len(content) > _MAX_DOC_BYTES:
            _log.warning(
                "intake %s: doc '%s' excede el cap de %d bytes (%d bytes), omitido",
                intake_id, name, _MAX_DOC_BYTES, len(content),
            )
            skipped_docs.append(name)
            continue
        # Sanitizar el nombre para evitar path-traversal dentro del docs_dir
        safe_name = Path(name).name  # solo el basename, sin directorios
        if not safe_name or safe_name.startswith("."):
            _log.warning("intake %s: nombre de doc inválido '%s', omitido", intake_id, name)
            skipped_docs.append(name)
            continue
        (docs_dir / safe_name).write_bytes(content)

    # Persistir fila en DB (SQL parametrizado)
    ensure_intake_table(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO intake_requests (client_id, brief_path, docs_dir, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                client_id,
                str(brief_path.relative_to(data_dir)),
                str(docs_dir.relative_to(data_dir)),
                _STATUS_PENDING,
                now,
            ),
        )
        conn.commit()
        row_id: int = cur.lastrowid  # type: ignore[assignment]
    finally:
        conn.close()

    # Intentar ingest ARIS4U (fail-open: el intake ya está creado)
    _try_ingest_aris4u(db_path, client_id, brief_text, intake_id)

    return row_id, skipped_docs


def _try_ingest_aris4u(
    db_path: Path,
    client_id: str,
    brief_text: str,
    intake_id: str,
) -> None:
    """Llama a ``session_manager.save_decision`` para que la memoria recuerde el intake.

    Fail-open: si cualquier import o llamada falla, loguea y continúa.  El
    intake ya quedó persistido en la DB; esta parte es best-effort.

    Args:
        db_path: Ruta a sessions.db (solo para resolver el PYTHONPATH si fuera
            necesario; save_decision ya conoce SESSIONS_DB vía config).
        client_id: Scope del cliente.
        brief_text: Texto del brief (se trunca a 800 chars para la decisión).
        intake_id: ID del intake (incluido en el rationale para trazabilidad).
    """
    try:
        # Importación local: el motor puede no estar disponible en tests sin el venv
        import sys as _sys
        _repo_root = Path(db_path).resolve().parents[1]
        if str(_repo_root) not in _sys.path:
            _sys.path.insert(0, str(_repo_root))
        from engine.v16 import session_manager as _sm  # type: ignore[import]

        _sm.save_decision(
            decision=f"[intake:{intake_id}] {brief_text[:800]}",
            rationale=f"Brief de intake recibido (intake_id={intake_id})",
            domain="intake",
            locked=False,
            client_id=client_id.lower(),
        )
    except Exception as exc:  # fail-open
        _log.warning(
            "intake %s: ingest ARIS4U falló (fail-open, intake ya creado): %s",
            intake_id, exc,
        )


# ---------------------------------------------------------------------------
# Lecturas
# ---------------------------------------------------------------------------

def list_intakes(
    db_path: str | Path,
    status: str | None = None,
) -> list[dict]:
    """Lista los intakes, opcionalmente filtrados por estado.

    Args:
        db_path: Ruta a sessions.db.
        status: Si se da, filtra por este estado exacto.  ``None`` = todos.

    Returns:
        Lista de dicts ``{id, client_id, brief_path, docs_dir, status, created_at}``,
        ordenados por ``created_at DESC``.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return []
    try:
        if status is not None:
            rows = conn.execute(
                "SELECT id, client_id, brief_path, docs_dir, status, created_at "
                "FROM intake_requests WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, client_id, brief_path, docs_dir, status, created_at "
                "FROM intake_requests ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # Tabla aún no creada
        return []
    finally:
        conn.close()


def get_intake(
    db_path: str | Path,
    intake_id: int,
) -> dict | None:
    """Devuelve un intake por su PK, o ``None`` si no existe.

    Args:
        db_path: Ruta a sessions.db.
        intake_id: PK del intake.

    Returns:
        Dict con las columnas del intake, o ``None``.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return None
    try:
        row = conn.execute(
            "SELECT id, client_id, brief_path, docs_dir, status, created_at "
            "FROM intake_requests WHERE id = ?",
            (intake_id,),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def set_status(
    db_path: str | Path,
    intake_id: int,
    status: str,
) -> None:
    """Actualiza el estado de un intake.

    Args:
        db_path: Ruta a sessions.db.
        intake_id: PK del intake a actualizar.
        status: Nuevo estado.  Debe ser uno de ``_VALID_STATUSES``.

    Raises:
        ValueError: Si el estado no está en la lista válida.
        sqlite3.Error: Si la actualización falla.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"status inválido '{status}'; válidos: {sorted(_VALID_STATUSES)}"
        )
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE intake_requests SET status = ? WHERE id = ?",
            (status, intake_id),
        )
        conn.commit()
    finally:
        conn.close()
