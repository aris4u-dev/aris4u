import re
import sqlite3
import json
import subprocess
import sys
from datetime import datetime, UTC
from pathlib import Path
from typing import Callable, Optional

from .config import SESSIONS_DB, BUSY_TIMEOUT_MS, MAX_FTS_RESULTS, OLLAMA_MAC_URL, EMBED_MODEL, EMBED_PREFIX


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(str(SESSIONS_DB))
    db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.row_factory = sqlite3.Row
    return db


def query_db(sql: str, params: tuple = (), fetch_all: bool = True) -> list[dict] | Optional[dict]:
    db = _connect()
    try:
        rows = db.execute(sql, params).fetchall()
        result = [dict(r) for r in rows]
        return result if fetch_all else (result[0] if result else None)
    except sqlite3.OperationalError:
        return [] if fetch_all else None
    finally:
        db.close()


def init_db() -> None:
    SESSIONS_DB.parent.mkdir(parents=True, exist_ok=True)
    db = _connect()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS digests (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            session_id TEXT,
            summary TEXT NOT NULL,
            built TEXT,
            decisions TEXT,
            failed TEXT,
            guards TEXT,
            pending TEXT,
            tags TEXT,
            embedding BLOB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_id TEXT REFERENCES digests(id),
            decision TEXT NOT NULL,
            rationale TEXT,
            domain TEXT,
            locked INTEGER DEFAULT 0,
            session_ref TEXT,
            evidence TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS guards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern TEXT NOT NULL,
            prevention TEXT NOT NULL,
            source_session TEXT,
            severity TEXT DEFAULT 'medium',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS digests_fts USING fts5(
            summary, built, decisions, failed, guards, pending, tags,
            content='digests', content_rowid='rowid'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
            decision, rationale, domain,
            content='decisions', content_rowid='rowid'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS guards_fts USING fts5(
            pattern, prevention,
            content='guards', content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS digests_ai AFTER INSERT ON digests BEGIN
            INSERT INTO digests_fts(rowid, summary, built, decisions, failed, guards, pending, tags)
            VALUES (new.rowid, new.summary, new.built, new.decisions, new.failed, new.guards, new.pending, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS decisions_ai AFTER INSERT ON decisions BEGIN
            INSERT INTO decisions_fts(rowid, decision, rationale, domain)
            VALUES (new.rowid, new.decision, new.rationale, new.domain);
        END;

        CREATE TRIGGER IF NOT EXISTS guards_ai AFTER INSERT ON guards BEGIN
            INSERT INTO guards_fts(rowid, pattern, prevention)
            VALUES (new.rowid, new.pattern, new.prevention);
        END;

        CREATE TABLE IF NOT EXISTS gate_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module_name TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            e2e_prompt TEXT,
            session_ref TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS gate_results_fts USING fts5(
            module_name, status, details,
            content='gate_results', content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS gate_results_ai AFTER INSERT ON gate_results BEGIN
            INSERT INTO gate_results_fts(rowid, module_name, status, details)
            VALUES (new.rowid, new.module_name, new.status, new.details);
        END;

        CREATE TABLE IF NOT EXISTS query_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            query_type TEXT,
            query_text TEXT,
            complexity_score REAL,
            computed_levels TEXT,
            actual_depth_used INTEGER,
            tokens_consumed INTEGER,
            decision_made INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_query_history_session
            ON query_history(session_id);

        CREATE TABLE IF NOT EXISTS v15_session_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        -- V18 Fase E: dueño PROPIO del texto de las observations (desacople de claude-mem.db,
        -- plugin 3er-party muerto desde 2026-05-18). El sidecar aris_vectors.db ya tiene los
        -- vectores; esta tabla guarda el TEXTO que _hydrate() necesita. La clave es `id`
        -- (= vec_map.source_id) → cada vector hidrata (paridad exacta: el histórico NO se
        -- dedup-a por content_hash, se perderían 26% de hidrataciones). El dedup 7.7x de
        -- claude-mem se corta en las escrituras FUTURAS (el mirror chequea content_hash).
        CREATE TABLE IF NOT EXISTS observations_local (
            id TEXT PRIMARY KEY,
            project TEXT,
            type TEXT,
            content TEXT NOT NULL,
            content_hash TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            verify_score REAL,
            client_id TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_obs_local_hash ON observations_local(content_hash);
        CREATE VIRTUAL TABLE IF NOT EXISTS observations_local_fts USING fts5(
            content, content='observations_local', content_rowid='rowid'
        );
        CREATE TRIGGER IF NOT EXISTS observations_local_ai AFTER INSERT ON observations_local BEGIN
            INSERT INTO observations_local_fts(rowid, content) VALUES (new.rowid, new.content);
        END;
    """)
    init_modules_table()
    init_engagements_table()
    _run_pending_migrations(db)
    db.close()


# ---------------------------------------------------------------------------
# D3 — Migration registry: each entry is (version_number, callable).
# _run_pending_migrations reads PRAGMA user_version and runs only the pending
# ones in order, then bumps user_version.  Idempotent: two runs are a no-op.
# ---------------------------------------------------------------------------

_MIGRATION_REGISTRY: list[tuple[int, "Callable[[sqlite3.Connection], None]"]] = []

# Register decorator — keeps ordering explicit and co-located.
def _register(version: int):  # noqa: ANN001
    def decorator(fn):  # noqa: ANN001, ANN202
        _MIGRATION_REGISTRY.append((version, fn))
        return fn
    return decorator


def _run_pending_migrations(db: sqlite3.Connection) -> None:
    """Gate migrations by PRAGMA user_version.

    Runs only the migrations whose version number is > current user_version,
    in ascending order. Sets user_version = target after each one so a crash
    mid-run is restartable. Idempotent: running twice is a no-op (second time
    user_version already equals target).
    """
    current: int = db.execute("PRAGMA user_version").fetchone()[0]
    pending = sorted(
        [(v, fn) for v, fn in _MIGRATION_REGISTRY if v > current],
        key=lambda t: t[0],
    )
    for version, fn in pending:
        fn(db)
        db.execute(f"PRAGMA user_version = {version}")
        db.commit()


@_register(1)
def _migrate_ws4_client_id(db: sqlite3.Connection) -> None:
    """WS4: Idempotent migration to add client_id column to decisions and guards.

    Checks if column exists; if not, adds it as nullable TEXT. Backward-compatible.
    """
    try:
        # Check if client_id already exists in decisions table
        info = db.execute("PRAGMA table_info(decisions)").fetchall()
        has_client_id = any(row[1] == 'client_id' for row in info)

        if not has_client_id:
            db.execute("ALTER TABLE decisions ADD COLUMN client_id TEXT DEFAULT NULL")
            # Re-create FTS trigger to include client_id in search
            db.execute("DROP TRIGGER IF EXISTS decisions_ai")
            db.execute("""CREATE TRIGGER decisions_ai AFTER INSERT ON decisions BEGIN
                INSERT INTO decisions_fts(rowid, decision, rationale, domain)
                VALUES (new.rowid, new.decision, new.rationale, new.domain);
            END;""")

        # V2.0 fix P0: digests también necesita client_id (save_digest lo inserta;
        # sin esta migración una instalación fresca crashea al persistir digests)
        info = db.execute("PRAGMA table_info(digests)").fetchall()
        has_client_id = any(row[1] == 'client_id' for row in info)
        if not has_client_id:
            db.execute("ALTER TABLE digests ADD COLUMN client_id TEXT DEFAULT NULL")

        # Check if client_id already exists in guards table
        info = db.execute("PRAGMA table_info(guards)").fetchall()
        has_client_id = any(row[1] == 'client_id' for row in info)

        if not has_client_id:
            db.execute("ALTER TABLE guards ADD COLUMN client_id TEXT DEFAULT NULL")

        db.commit()
    except sqlite3.OperationalError as e:
        # Column already exists or other error—idempotent, continue
        if "duplicate column name" not in str(e).lower():
            pass  # Log silently, don't crash


@_register(2)
def _migrate_taxonomy(db: sqlite3.Connection) -> None:
    """Taxonomía de memoria (2026-06-22): añade los 2 ejes nuevos a la memoria.

    - decisions: ``mem_type`` (fact/rule/decision/episode) + ``epistemic_status``
      (confirmed/refuted/provisional/open_question/superseded).
    - guards: ``epistemic_status`` (las reglas activas = confirmed por defecto).

    Idempotente y aditivo (no toca CREATE TABLE; corre en cada init_db). El backfill
    ``locked=1 -> 'confirmed'`` se ejecuta SOLO al crear la columna (no en cada init_db)
    para NO pisar reclasificaciones posteriores. Diseño: architecture/MEMORY_TAXONOMY_PROPOSAL.md.
    """
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(decisions)").fetchall()}
        if 'mem_type' not in cols:
            db.execute("ALTER TABLE decisions ADD COLUMN mem_type TEXT DEFAULT NULL")
        if 'epistemic_status' not in cols:
            db.execute("ALTER TABLE decisions ADD COLUMN epistemic_status TEXT DEFAULT 'provisional'")
            # Backfill UNA vez: lo que hoy está lockeado es la verdad confiable = confirmed.
            db.execute("UPDATE decisions SET epistemic_status='confirmed' WHERE locked=1")
        gcols = {row[1] for row in db.execute("PRAGMA table_info(guards)").fetchall()}
        if 'epistemic_status' not in gcols:
            db.execute("ALTER TABLE guards ADD COLUMN epistemic_status TEXT DEFAULT 'confirmed'")
        db.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            pass  # idempotente


@_register(3)
def _migrate_method_atoms(db: sqlite3.Connection) -> None:
    """Átomos de método (2026-06-22): 3 ejes estructurales + campos del átomo.

    Indexa una decisión como modelo/método reutilizable por la ESTRUCTURA del
    problema (no su dominio), para detectar transferencia. Ejes en
    ``engine/v16/method_atom_vocab.py``. Idempotente y aditivo (corre en cada
    init_db; ninguna columna se rellena retroactivamente). Diseño: memoria
    project_atom_method_engine.

    Columnas (todas TEXT NULL):
        problem_class   Eje 1 — estructura del problema del mundo.
        artifact_type   Eje 2 — patrón de la solución de software (opcional).
        regime          Eje 3 — predictibilidad (pure-random no se ingiere).
        skeleton        JSON: leyes/métodos del modelo.
        variable_verdicts  JSON: [{var, verdict KEEP/DEPENDS/DISCARD, reason}].
        validity_domain Texto: dónde aplica y dónde rompe el esqueleto.
        transfers_to    JSON: [{target_class, rel, condicion}] (esqueleto, no calibración).
        structural_signature  Firma "problem_class|artifact_type|regime" para dedup
            (caso real: 71 átomos crudos = ~22 únicos; sin firma el repo se llena de redundancia).
        canonical_id    Si la fila es una instancia derivada, id del espécimen canónico
            (NULL = es canónica o aún sin deduplicar).
    """
    new_cols = (
        "problem_class", "artifact_type", "regime",
        "skeleton", "variable_verdicts", "validity_domain", "transfers_to",
        "structural_signature",
    )
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(decisions)").fetchall()}
        for col in new_cols:
            if col not in cols:
                db.execute(f"ALTER TABLE decisions ADD COLUMN {col} TEXT DEFAULT NULL")
        if "canonical_id" not in cols:
            db.execute("ALTER TABLE decisions ADD COLUMN canonical_id INTEGER DEFAULT NULL")
        db.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            pass  # idempotente


@_register(4)
def _migrate_atom_axes(db: sqlite3.Connection) -> None:
    """Ejes adoption/evidence (2026-06-22): separar "qué método existe" de "qué se usa".

    Revelado por un catálogo real (~92 métodos, solo ~5% usados): ``have/naive/gap``
    mezclaba madurez-del-conocimiento con estado-de-adopción. Dos campos ortogonales:
        adoption       used | used-naive | unused | gap-no-method-exists
        evidence_kind  calibrated (medido en runtime) | catalog (conocimiento puro verificado)
    Hace consultable el espacio no-explorado: "dame los unused+catalog de class=X".
    Vocabulario en method_atom_vocab. Idempotente y aditivo.
    """
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(decisions)").fetchall()}
        if "adoption" not in cols:
            db.execute("ALTER TABLE decisions ADD COLUMN adoption TEXT DEFAULT NULL")
        if "evidence_kind" not in cols:
            db.execute("ALTER TABLE decisions ADD COLUMN evidence_kind TEXT DEFAULT NULL")
        # Provenance de átomo (2026-06-23): proyecto de ORIGEN, alimenta el grafo de
        # transferencia. Antes era one-off (Fase 2); aquí lo hacemos idempotente para que
        # DBs frescas (tests/nuevos devs) tengan la columna y save_decision no rompa.
        if "source_project" not in cols:
            db.execute("ALTER TABLE decisions ADD COLUMN source_project TEXT DEFAULT NULL")
        db.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            pass  # idempotente


@_register(5)
def _migrate_trust_source(db: sqlite3.Connection) -> None:
    """D1 — Batch D: anti-poisoning trust_source column (2026-07-06).

    Adds ``trust_source TEXT DEFAULT 'user'`` to decisions so audit-findings
    ingested by aris-client-audit (trust_source='audit') are distinguishable
    from genuine user decisions.  Default 'user' preserves all existing rows
    without backfill.  Idempotent and additive.
    """
    try:
        cols = {row[1] for row in db.execute("PRAGMA table_info(decisions)").fetchall()}
        if "trust_source" not in cols:
            db.execute("ALTER TABLE decisions ADD COLUMN trust_source TEXT DEFAULT 'user'")
        db.commit()
    except sqlite3.OperationalError as e:
        if "duplicate column name" not in str(e).lower():
            pass  # idempotente


@_register(6)
def _migrate_amplification_scores(db: sqlite3.Connection) -> None:
    """E3 — Batch E: per-session amplification_score table (2026-07-06).

    Stores the amplification_score computed at each SessionEnd.  Additive,
    idempotent.  Signals available at write-time: recalls_useful/recalls_total
    (from recall_feedback JOIN recall_events).  Other signals (f1_labels,
    capabilities_adopted, guard_blocks) are zero until their tracking is wired
    to persistent per-session storage — surfaced honestly via signals_note.
    """
    db.execute("""
        CREATE TABLE IF NOT EXISTS amplification_scores (
            session_id TEXT PRIMARY KEY,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            recalls_useful INTEGER NOT NULL DEFAULT 0,
            recalls_total INTEGER NOT NULL DEFAULT 0,
            f1_useful INTEGER NOT NULL DEFAULT 0,
            f1_total INTEGER NOT NULL DEFAULT 0,
            capabilities_adopted INTEGER NOT NULL DEFAULT 0,
            guard_blocks INTEGER NOT NULL DEFAULT 0,
            total_turns INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0.0,
            signals_note TEXT
        )
    """)


@_register(7)
def _migrate_audit_chain_genesis(db: sqlite3.Connection) -> None:
    """F2 — Batch F: audit hash-chain genesis record (2026-07-06).

    Stores metadata for the EU AI Act Art.12 tamper-evident hash chain.
    Single-row table (id=1 enforced).  Populated by ``audit_export.py``
    on first ``--init`` call or automatically when the chain head is found.
    Additive and idempotent — safe to run on existing DBs.
    """
    db.execute("""
        CREATE TABLE IF NOT EXISTS audit_chain_genesis (
            id         INTEGER PRIMARY KEY CHECK(id = 1),
            genesis_ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            genesis_hash TEXT NOT NULL,
            note       TEXT
        )
    """)


def save_digest(
    digest_id: str,
    summary: str,
    built: str = "",
    decisions: str = "",
    failed: str = "",
    guards: str = "",
    pending: str = "",
    tags: str = "",
    session_id: str = "",
    client_id: Optional[str] = None,
) -> None:
    """Persist a session digest. client_id auto-populates from ARIS4U_CLIENT /
    cwd (via detect_client) when None, so per-client recall sees session closes."""
    if client_id is None:
        client_id = detect_client()
    db = _connect()
    date = digest_id[:10] if len(digest_id) >= 10 else datetime.now().strftime("%Y-%m-%d")
    db.execute(
        """INSERT OR REPLACE INTO digests
           (id, date, session_id, summary, built, decisions, failed, guards, pending, tags, client_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (digest_id, date, session_id, summary, built, decisions, failed, guards, pending, tags, client_id),
    )
    db.commit()
    db.close()


KNOWN_CLIENT_PROJECTS: dict[str, str] = {
    # Proyectos top-level (fuera de 03-clients) que pertenecen a un cliente.
    # Vacío por defecto — agrega tus propios proyectos top-level aquí o en config.json.
    # Ejemplo: "mi-proyecto": "mi-proyecto"
}


def resolve_client_from_path(path: Optional[str] = None) -> Optional[str]:
    """Resuelve el client_id canónico (SIEMPRE lower-case) desde una ruta del filesystem.

    Reglas en orden: (1) ~/projects/03-clients/<cliente>/ -> nombre de carpeta en lower-case
    quitando sufijos conocidos (-platform/-website/-app/-web): cliente-platform -> cliente,
    acme-wellness -> acme-wellness (NUNCA partir en el primer guion: bug V2.0 'acme');
    (2) proyecto top-level conocido (ver KNOWN_CLIENT_PROJECTS, vacío por defecto);
    (3) marcador <repo>/.aris-client (un dev externo declara su cliente; BYO horizontal).
    None si la ruta no pertenece a ningun cliente.
    """
    import os
    p = path or os.getcwd()
    m = re.search(r"/projects/03-clients/([^/]+)", p)
    if m:
        name = m.group(1).lower()
        for suffix in ("-platform", "-website", "-app", "-web"):
            name = name.removesuffix(suffix)
        return name
    m = re.search(r"/projects/([^/]+)", p)
    if m and m.group(1).lower() in KNOWN_CLIENT_PROJECTS:
        return KNOWN_CLIENT_PROJECTS[m.group(1).lower()]
    d = p
    for _ in range(6):  # buscar marcador .aris-client hacia arriba
        try:
            marker = os.path.join(d, ".aris-client")
            if os.path.isfile(marker):
                with open(marker) as f:
                    val = f.read().strip().lower()
                if val:
                    return val
        except Exception:
            pass
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    return None


def _client_from_session_bridge() -> Optional[str]:
    """Lee el cliente activo del puente de sesion (escrito por los hooks que SI ven el cwd).

    El servidor MCP es un demonio de larga vida con cwd neutro (~), asi que no puede
    detectar el cliente por su propio cwd. Los hooks (depth_inject/lab_session_init)
    escriben el puente cada turno; aqui lo leemos con TTL de 1h.

    POR-SESION (fix P0 cross-client leak): cada sesion usa su propio archivo, indexado por
    CLAUDE_CODE_SESSION_ID (que tanto el daemon MCP como los hooks heredan). Dos sesiones
    concurrentes en clientes distintos NO pueden contaminarse. Solo se cae al archivo global
    si NO hay session id (ejecucion manual/test); con session id se usa EXCLUSIVAMENTE el suyo.
    """
    import os
    import json
    import time
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    bridge = f"/tmp/aris4u_active_client.{sid}.json" if sid else "/tmp/aris4u_active_client.json"
    try:
        if not os.path.isfile(bridge):
            return None
        if (time.time() - os.path.getmtime(bridge)) > 3600:  # stale -> ignorar
            return None
        with open(bridge) as f:
            data = json.load(f)
        c = (data.get("client_id") or "").strip().lower()
        return c or None
    except Exception:
        return None


def detect_client() -> Optional[str]:
    """Cliente activo, canonico (lower-case). Orden: ARIS4U_CLIENT env -> cwd -> puente de sesion.

    Mirrors hooks/depth_inject.sh (e.g. 03-clients/cliente-platform -> cliente).
    """
    import os
    c = os.environ.get("ARIS4U_CLIENT")
    if c and c.strip():
        return c.strip().lower()
    c = resolve_client_from_path(os.getcwd())
    if c:
        return c
    return _client_from_session_bridge()


def _project_from_path(p: str) -> Optional[str]:
    """Nombre EXACTO de la carpeta de proyecto bajo ~/projects/[NN-categoria/]<proyecto>/.

    Sin canonicalizar (a diferencia de resolve_client_from_path): mi-app se queda
    mi-app, mi-platform se queda mi-platform. None si no está bajo /projects/.
    """
    m = re.search(r"/projects/(?:\d\d-[^/]+/)?([^/]+)", p)
    return m.group(1) if m else None


def _cwd_from_session_bridge() -> Optional[str]:
    """Lee el cwd que el puente de sesión guardó (para el daemon MCP, que tiene cwd neutro).

    Espeja _client_from_session_bridge pero devuelve el campo ``cwd`` con el mismo TTL 1h y
    aislamiento por CLAUDE_CODE_SESSION_ID. Permite derivar source_project en el daemon MCP.
    """
    import os
    import json
    import time
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    bridge = f"/tmp/aris4u_active_client.{sid}.json" if sid else "/tmp/aris4u_active_client.json"
    try:
        if not os.path.isfile(bridge) or (time.time() - os.path.getmtime(bridge)) > 3600:
            return None
        with open(bridge) as f:
            data = json.load(f)
        cwd = (data.get("cwd") or "").strip()
        return cwd or None
    except Exception:
        return None


def detect_source_project(path: Optional[str] = None) -> Optional[str]:
    """Proyecto de ORIGEN de un átomo/decisión: carpeta del repo SIN canonicalizar.

    A diferencia de client_id (cliente-platform→cliente), conserva el nombre EXACTO de
    la carpeta (mi-proyecto, aris4u) para rastrear de dónde se minó/aprendió el patrón
    — el eje que alimenta el grafo de transferencia entre proyectos. Orden: ARIS4U_SOURCE_PROJECT
    env → cwd propio → cwd del puente de sesión (el daemon MCP tiene cwd neutro). None si la
    ruta no está bajo ~/projects/.
    """
    import os
    env = os.environ.get("ARIS4U_SOURCE_PROJECT")
    if env and env.strip():
        return env.strip()
    proj = _project_from_path(path or os.getcwd())
    if proj:
        return proj
    cwd = _cwd_from_session_bridge()
    return _project_from_path(cwd) if cwd else None


def save_decision(decision: str, rationale: str = "", domain: str = "",
                   digest_id: str = "", locked: bool = False,
                   session_ref: str = "", evidence: str = "", client_id: str | None = None,
                   mem_type: str | None = None,
                   problem_class: str | None = None, artifact_type: str | None = None,
                   regime: str | None = None, skeleton: str | None = None,
                   variable_verdicts: str | None = None, validity_domain: str | None = None,
                   transfers_to: str | None = None, structural_signature: str | None = None,
                   canonical_id: int | None = None, adoption: str | None = None,
                   evidence_kind: str | None = None,
                   source_project: str | None = None,
                   trust_source: str = "user") -> None:
    """Save a decision with optional client_id. Auto-populate from ARIS4U_CLIENT env var if None.

    Args:
        decision: The decision text
        rationale: Why this decision was made
        domain: Domain area (auth, database, security, etc.)
        digest_id: Associated digest ID
        locked: Whether this decision is locked
        session_ref: Session reference
        evidence: Evidence supporting the decision
        client_id: Client identifier (auto-populated from ARIS4U_CLIENT if None)
        mem_type: Taxonomy axis (fact/rule/decision/episode/provenance). Provenance
            (e.g. git-commits) is excluded from active recall by both channels.
        problem_class: Método-atom eje 1 — estructura del problema (method_atom_vocab).
        artifact_type: Método-atom eje 2 — patrón de software (opcional).
        regime: Método-atom eje 3 — predictibilidad (deterministic/.../pure-random).
        skeleton: JSON con las leyes/métodos del modelo.
        variable_verdicts: JSON [{var, verdict, reason}] (KEEP/DEPENDS/DISCARD).
        validity_domain: Texto: dónde aplica y dónde rompe el esqueleto.
        transfers_to: JSON [{target_class, rel, condicion}] — transfiere el esqueleto, no la calibración.
        structural_signature: Firma "problem_class|artifact_type|regime" para dedup.
        canonical_id: id del espécimen canónico si esta fila es instancia derivada (NULL = canónica).
        source_project: Proyecto de ORIGEN (carpeta del repo, sin canonicalizar: mi-proyecto,
            aris4u). Auto-poblado vía detect_source_project (env→cwd→puente) si None,
            así los átomos FUTUROS llevan origen sin backfill. Alimenta el grafo de transferencia.
        trust_source: Procedencia de la decisión. 'user' (default) = decisión genuina del
            usuario; 'audit' = hallazgo de aris-client-audit u otra fuente automática.
            El recall prefija las entradas 'audit' con '[audit]' para evitar confusión.
    """
    if client_id is None:
        client_id = detect_client()
    if source_project is None:
        source_project = detect_source_project()

    db = _connect()
    db.execute(
        "INSERT INTO decisions (digest_id, decision, rationale, domain, locked, session_ref, "
        "evidence, client_id, mem_type, problem_class, artifact_type, regime, skeleton, "
        "variable_verdicts, validity_domain, transfers_to, structural_signature, canonical_id, "
        "adoption, evidence_kind, source_project, trust_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (digest_id, decision, rationale, domain, int(locked), session_ref, evidence,
         client_id, mem_type, problem_class, artifact_type, regime, skeleton,
         variable_verdicts, validity_domain, transfers_to, structural_signature, canonical_id,
         adoption, evidence_kind, source_project, trust_source),
    )
    db.commit()
    row_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.close()
    _async_embed_decision(row_id, f"{decision} {rationale} {domain}", client_id or "")


def find_duplicate_atoms(structural_signature: str, client_id: str | None = None,
                         limit: int = 5) -> list[dict]:
    """Busca átomos existentes con la misma firma estructural (candidatos a dedup).

    Permite que la ingesta avise de una posible duplicación (misma estructura
    problem_class|artifact_type|regime) para que el humano/Claude decida canónico-vs-
    instancia, en vez de duplicar en silencio (caso real: 71 crudos = ~22 únicos). Busca
    dentro del MISMO scope (``client_id IS ?`` es NULL-safe).

    Args:
        structural_signature: Firma del átomo entrante (method_atom_vocab.structural_signature).
        client_id: Scope del cliente (None = scope global / sin cliente).
        limit: Máximo de candidatos a devolver.

    Returns:
        Lista de dicts {id, decision, problem_class, artifact_type, canonical_id}.
    """
    db = _connect()
    try:
        rows = db.execute(
            "SELECT id, decision, problem_class, artifact_type, canonical_id "
            "FROM decisions WHERE structural_signature = ? AND client_id IS ? "
            "ORDER BY id LIMIT ?",
            (structural_signature, client_id, limit),
        ).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]


def find_atoms(problem_class: str | None = None, adoption: str | None = None,
               evidence_kind: str | None = None, client_id: str | None = None,
               limit: int = 20) -> list[dict]:
    """Consulta el repositorio de átomos de método por estructura y/o adopción.

    Materializa la visión 'repositorio de opciones': p.ej.
    ``find_atoms(problem_class='combinatorial-optimization', adoption='unused')`` =
    "dame todos los métodos válidos NO usados de esta clase para elegir el mejor".
    Los filtros se combinan con AND; los ``None`` se ignoran. Solo átomos (mem_type='fact').

    Args:
        problem_class: filtra por estructura del problema (eje 1).
        adoption: estado de adopción (used/used-naive/unused/gap-no-method-exists).
        evidence_kind: origen de la evidencia (calibrated/catalog).
        client_id: scope; ``None`` = no filtra por cliente.
        limit: máximo de resultados.

    Returns:
        Lista de dicts con los campos clave del átomo.
    """
    # Dedup lógico: excluir átomos no-canónicos (canonical_id apunta a su canónico) para que
    # el recall NO traiga copias del mismo patrón. Ver dedup vía canonical_id (2026-06-29).
    # FIX #7: canonical_id es INTEGER → canonical_id = '' es siempre falso (dead code).
    clauses, params = ["mem_type = 'fact'",
                       "(canonical_id IS NULL OR canonical_id = id)"], []
    for col, val, op in (("problem_class", problem_class, "="), ("adoption", adoption, "="),
                         ("evidence_kind", evidence_kind, "="), ("client_id", client_id, "IS")):
        if val is not None:
            clauses.append(f"{col} {op} ?")
            params.append(val)
    params.append(limit)
    db = _connect()
    try:
        rows = db.execute(
            "SELECT id, decision, problem_class, artifact_type, regime, adoption, evidence_kind, "
            "validity_domain, transfers_to FROM decisions WHERE " + " AND ".join(clauses) +
            " ORDER BY id LIMIT ?", params,
        ).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]


def get_locked_decisions(query: str = "", limit: Optional[int] = 10) -> list[dict]:
    db = _connect()
    if query:
        fts_query = " OR ".join(query.split())
        try:
            sql = """SELECT d.decision, d.rationale, d.domain, d.session_ref, d.evidence
                   FROM decisions d
                   WHERE d.locked = 1 AND d.rowid IN
                   (SELECT rowid FROM decisions_fts WHERE decisions_fts MATCH ?)"""
            params: list = [fts_query]
            if limit is not None:
                sql += " LIMIT ?"
                params.append(limit)
            rows = db.execute(sql, params).fetchall()
            db.close()
            return [dict(r) for r in rows]
        except Exception:
            pass
    sql = "SELECT decision, rationale, domain, session_ref, evidence FROM decisions WHERE locked = 1 ORDER BY created_at DESC"
    params: list = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    rows = db.execute(sql, params).fetchall()
    db.close()
    return [dict(r) for r in rows]


def save_guard(pattern: str, prevention: str, source_session: str = "", severity: str = "medium", client_id: str | None = None) -> None:
    """Save a guard with optional client_id. Auto-populate from ARIS4U_CLIENT env var if None.

    Args:
        pattern: Guard pattern to detect
        prevention: Prevention text
        source_session: Source session reference
        severity: Severity level (low, medium, high, critical)
        client_id: Client identifier (auto-populated from ARIS4U_CLIENT if None)
    """
    if client_id is None:
        client_id = detect_client()

    db = _connect()
    db.execute(
        "INSERT INTO guards (pattern, prevention, source_session, severity, client_id) VALUES (?, ?, ?, ?, ?)",
        (pattern, prevention, source_session, severity, client_id),
    )
    db.commit()
    db.close()


def search(query: str, limit: int = MAX_FTS_RESULTS, client_id: Optional[str] = None) -> dict:
    db = _connect()
    results = {"digests": [], "decisions": [], "guards": []}

    fts_query = " OR ".join(query.split())

    # Paridad con el canal semántico (semantic_recall): el keyword FTS5 NO debe inyectar
    # como guía vigente memoria muerta (refuted/superseded) ni provenance (git-commits),
    # ni fugar decisiones de otros clientes. Soft-scope: cliente pedido + sin-dueño ('' / NULL).
    # NULL-safe a propósito: un `NOT IN` plano descartaría filas legítimas con la columna NULL.
    # A0.2 fix: client_id="" (sentinel unscoped) → solo sin-dueño; None → global (aris_search).
    if client_id:
        scope_sql = " AND ({a}.client_id = ? OR {a}.client_id = '' OR {a}.client_id IS NULL)"
        scope_params: tuple = (client_id,)
    elif client_id == "":  # sentinel → unscoped-only, sin fuga cross-client
        scope_sql = " AND ({a}.client_id = '' OR {a}.client_id IS NULL)"
        scope_params = ()
    else:
        scope_sql = ""
        scope_params = ()

    try:
        rows = db.execute(
            "SELECT d.id, d.date, d.summary, d.decisions, d.guards FROM digests d "
            "WHERE d.rowid IN (SELECT rowid FROM digests_fts WHERE digests_fts MATCH ?)"
            + scope_sql.format(a="d") + " LIMIT ?",
            (fts_query, *scope_params, limit),
        ).fetchall()
        results["digests"] = [dict(r) for r in rows]
    except Exception:
        pass

    try:
        epistemic_sql = (
            " AND (d.epistemic_status IS NULL OR d.epistemic_status NOT IN ('refuted','superseded'))"
            " AND (d.mem_type IS NULL OR d.mem_type != 'provenance')"
            # Dedup lógico: para facts excluir copias no-canónicas (2026-06-29).
            # FIX #7: canonical_id = '' removido (INTEGER, siempre falso — dead code).
            " AND (d.mem_type != 'fact' OR d.canonical_id IS NULL OR d.canonical_id = d.id)"
        )
        rows = db.execute(
            "SELECT d.decision, d.rationale, d.domain, d.trust_source FROM decisions d "
            "WHERE d.rowid IN (SELECT rowid FROM decisions_fts WHERE decisions_fts MATCH ?)"
            + epistemic_sql + scope_sql.format(a="d") + " LIMIT ?",
            (fts_query, *scope_params, limit),
        ).fetchall()
        results["decisions"] = [dict(r) for r in rows]
    except Exception:
        pass

    try:
        rows = db.execute(
            "SELECT g.pattern, g.prevention, g.severity FROM guards g "
            "WHERE g.rowid IN (SELECT rowid FROM guards_fts WHERE guards_fts MATCH ?)"
            + scope_sql.format(a="g") + " LIMIT ?",
            (fts_query, *scope_params, limit),
        ).fetchall()
        results["guards"] = [dict(r) for r in rows]
    except Exception:
        pass

    db.close()

    # WS3: semantic layer is served by the sqlite-vec sidecar — it supersedes the legacy
    # brute-force cosine over decisions.embedding (a column that no longer exists, H48).
    results["semantic"] = semantic_recall(query, limit=limit, client_id=client_id)

    return results


def get_recent_digests(limit: int = 5) -> list[dict]:
    db = _connect()
    rows = db.execute(
        "SELECT id, date, summary, decisions, guards, pending FROM digests ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_all_guards() -> list[dict]:
    db = _connect()
    rows = db.execute(
        "SELECT pattern, prevention, severity, source_session FROM guards ORDER BY created_at DESC"
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def save_query_history(
    session_id: str,
    query_type: str,
    query_text: str,
    complexity_score: float,
    computed_levels: list[int],
) -> None:
    db = _connect()
    try:
        db.execute(
            """INSERT INTO query_history
               (session_id, query_type, query_text, complexity_score, computed_levels)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, query_type, query_text[:500], complexity_score, json.dumps(computed_levels)),
        )
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


def get_stats() -> dict:
    db = _connect()
    stats = {
        "digests": db.execute("SELECT count(*) FROM digests").fetchone()[0],
        "decisions": db.execute("SELECT count(*) FROM decisions").fetchone()[0],
        "guards": db.execute("SELECT count(*) FROM guards").fetchone()[0],
    }
    db.close()
    return stats


def get_modules_completed() -> list[str]:
    """Return all distinct module names ever recorded in `modules_completed`.

    NOTE: name is historical — this returns every module_name ever registered,
    NOT only modules currently in PASS status. For current status filtering,
    use `get_module_results(module_name=...)` which returns the full row.
    """
    db = _connect()
    try:
        rows = db.execute("SELECT DISTINCT module_name FROM modules_completed").fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()


def get_current_module() -> Optional[str]:
    db = _connect()
    try:
        row = db.execute(
            "SELECT module_name FROM modules_completed ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None
    finally:
        db.close()


def init_engagements_table() -> None:
    db = _connect()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS engagements (
            id TEXT PRIMARY KEY,
            client_name TEXT NOT NULL,
            target TEXT NOT NULL,
            scope TEXT,
            phase TEXT DEFAULT 'scope',
            status TEXT DEFAULT 'active',
            findings_critical INTEGER DEFAULT 0,
            findings_high INTEGER DEFAULT 0,
            findings_medium INTEGER DEFAULT 0,
            findings_low INTEGER DEFAULT 0,
            findings_info INTEGER DEFAULT 0,
            progress_pct INTEGER DEFAULT 0,
            eta TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS engagement_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id TEXT REFERENCES engagements(id),
            title TEXT NOT NULL,
            severity TEXT NOT NULL,
            category TEXT,
            description TEXT,
            remediation TEXT,
            cvss REAL,
            status TEXT DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS engagement_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engagement_id TEXT REFERENCES engagements(id),
            message TEXT NOT NULL,
            phase TEXT,
            visible_to_client INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    db.close()


def create_engagement(engagement_id: str, client_name: str, target: str, scope: str = "") -> dict:
    db = _connect()
    db.execute(
        "INSERT INTO engagements (id, client_name, target, scope) VALUES (?, ?, ?, ?)",
        (engagement_id, client_name, target, scope),
    )
    db.commit()
    row = db.execute("SELECT * FROM engagements WHERE id = ?", (engagement_id,)).fetchone()
    db.close()
    return dict(row)


def update_engagement(engagement_id: str, **kwargs) -> dict:
    db = _connect()
    valid_fields = {"phase", "status", "findings_critical", "findings_high", "findings_medium",
                    "findings_low", "findings_info", "progress_pct", "eta", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in valid_fields}
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [engagement_id]
        db.execute(f"UPDATE engagements SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?", values)
        db.commit()
    row = db.execute("SELECT * FROM engagements WHERE id = ?", (engagement_id,)).fetchone()
    db.close()
    return dict(row) if row else {}


def get_engagement(engagement_id: str) -> Optional[dict]:
    db = _connect()
    row = db.execute("SELECT * FROM engagements WHERE id = ?", (engagement_id,)).fetchone()
    db.close()
    return dict(row) if row else None


def list_engagements(status: str = "active") -> list[dict]:
    db = _connect()
    rows = db.execute("SELECT * FROM engagements WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
    db.close()
    return [dict(r) for r in rows]


def add_finding(engagement_id: str, title: str, severity: str, category: str = "",
                description: str = "", remediation: str = "", cvss: float = 0.0) -> int | None:
    db = _connect()
    cursor = db.execute(
        "INSERT INTO engagement_findings (engagement_id, title, severity, category, description, remediation, cvss) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (engagement_id, title, severity, category, description, remediation, cvss),
    )
    sev_col = f"findings_{severity.lower()}"
    if sev_col in ("findings_critical", "findings_high", "findings_medium", "findings_low", "findings_info"):
        db.execute(f"UPDATE engagements SET {sev_col} = {sev_col} + 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (engagement_id,))
    db.commit()
    finding_id = cursor.lastrowid
    db.close()
    return finding_id


def get_findings(engagement_id: str, include_details: bool = False) -> list[dict]:
    db = _connect()
    if include_details:
        rows = db.execute(
            "SELECT * FROM engagement_findings WHERE engagement_id = ? ORDER BY cvss DESC", (engagement_id,)
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, title, severity, category, cvss, status FROM engagement_findings WHERE engagement_id = ? ORDER BY cvss DESC",
            (engagement_id,),
        ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def add_engagement_log(engagement_id: str, message: str, phase: str = "", visible_to_client: bool = True) -> None:
    db = _connect()
    db.execute(
        "INSERT INTO engagement_log (engagement_id, message, phase, visible_to_client) VALUES (?, ?, ?, ?)",
        (engagement_id, message, phase, int(visible_to_client)),
    )
    db.commit()
    db.close()


def get_engagement_log(engagement_id: str, client_view: bool = False) -> list[dict]:
    db = _connect()
    if client_view:
        rows = db.execute(
            "SELECT message, phase, created_at FROM engagement_log WHERE engagement_id = ? AND visible_to_client = 1 ORDER BY created_at DESC LIMIT 50",
            (engagement_id,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM engagement_log WHERE engagement_id = ? ORDER BY created_at DESC LIMIT 100",
            (engagement_id,),
        ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def get_client_view(engagement_id: str) -> Optional[dict]:
    eng = get_engagement(engagement_id)
    if not eng:
        return None
    findings = get_findings(engagement_id, include_details=False)
    log = get_engagement_log(engagement_id, client_view=True)
    return {
        "client": eng["client_name"],
        "target": eng["target"],
        "phase": eng["phase"],
        "status": eng["status"],
        "progress": eng["progress_pct"],
        "eta": eng.get("eta", ""),
        "findings": {
            "critical": eng["findings_critical"],
            "high": eng["findings_high"],
            "medium": eng["findings_medium"],
            "low": eng["findings_low"],
            "info": eng["findings_info"],
            "total": eng["findings_critical"] + eng["findings_high"] + eng["findings_medium"] + eng["findings_low"] + eng["findings_info"],
        },
        "finding_titles": [{"title": f["title"], "severity": f["severity"], "cvss": f["cvss"]} for f in findings],
        "log": log[:20],
    }


def embed_text(text: str, role: str = "doc") -> Optional[list[float]]:
    """Embed text vía el embedder Mac-local, aplicando el prefijo de tarea del modelo.

    Args:
        text: Texto a embeber (truncado a 4000 chars).
        role: 'doc' para indexar, 'query' para consultar. Modelos asimétricos
            (EmbeddingGemma, arctic) exigen prefijos distintos por rol; bge-m3 no.

    Returns:
        El vector de embedding, o None si el embedder falla.
    """
    prefix = EMBED_PREFIX.get(EMBED_MODEL, {}).get(role, "")
    try:
        result = subprocess.run(
            ["curl", "-s", f"{OLLAMA_MAC_URL}/api/embeddings",
             "-d", json.dumps({"model": EMBED_MODEL, "prompt": prefix + text[:4000], "keep_alive": "30m"})],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(result.stdout)
        return data.get("embedding")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
        return None


def _humanize_embedding_text(text: str) -> str:
    """Transform ``name:<slug> | <content>`` decisions for better embedding quality.

    Decisions stored as ``name:<slug> | <content>`` carry a machine-readable slug
    that the embedding model cannot match to natural-language queries (e.g. a query
    for "stripe customer id migration" misses ``name:stripe-external-id-on-profile``).
    This function returns a version where the slug is humanized — hyphens/underscores
    replaced by spaces — as a leading phrase before the content, making the vector
    representation queryable without modifying the original DB text.

    Args:
        text: Raw decision text, possibly in ``name:<slug> | <content>`` form.

    Returns:
        If the pattern matches, ``"<humanized slug>\\n<content>"``; otherwise
        ``text`` unchanged.
    """
    m = re.match(r"^name:([a-z0-9_-]+) \| (.+)$", text, re.DOTALL)
    if not m:
        return text
    slug = m.group(1).replace("-", " ").replace("_", " ")
    return f"{slug}\n{m.group(2)}"


def _async_embed_decision(row_id: int, text: str, client_id: str = "") -> None:
    """Embed a decision in background (non-blocking): legacy blob + WS3 sidecar index."""
    import threading

    def _embed():
        emb = embed_text(_humanize_embedding_text(text))
        if not emb:
            return
        db = _connect()
        try:
            db.execute("UPDATE decisions SET embedding = ? WHERE id = ?",
                       (json.dumps(emb).encode(), row_id))
            db.commit()
        except Exception:
            # decisions.embedding column may not exist in older DBs (schema drift H48)
            pass
        finally:
            db.close()
        # WS3 dual-write: index into the sqlite-vec sidecar, reusing the embedding.
        try:
            from . import vector_store
            vector_store._upsert("decisions", str(row_id), emb,
                                 client_id or "", "decision", vector_store._hash(text))
        except Exception:
            pass

    threading.Thread(target=_embed, daemon=True).start()


def _hydrate(
    source: str, source_id: str
) -> Optional[tuple[str, Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[object]]]:
    """Fetch ``(texto, epistemic_status, mem_type, problem_class, validity_domain, structural_signature, canonical_id)`` de un hit.

    Los 6 campos extra solo existen para decisions (taxonomía 2026-06 + átomos de
    método 2026-06-22 + dedup canónico 2026-06-29); las observations devuelven ``None`` en
    todos. Permite al recall filtrar/etiquetar por confianza y marcar átomos de método
    (piso-no-techo) sin una query extra. ``structural_signature`` ("problem_class|artifact_type|regime")
    es el marcador de átomo MÁS amplio: cubre los 146 átomos boostables (vs 75 que solo
    tienen problem_class) — incluidos los operacionales (surge/idempotency/FSM) que
    NO llevan problem_class. ``canonical_id`` es el id del espécimen canónico si esta fila es
    una copia derivada (NULL / '' / id_propio = canónica). Único llamador: semantic_recall.
    Read-only.
    """
    try:
        if source == "observations":
            # V18 Fase E: texto PROPIO (observations_local). Desacople completado (paso 10,
            # 2026-07-02): claude-mem.db 3er-party archivada; sin fallback. id = vec_map.source_id.
            con = _connect()
            row = con.execute(
                "SELECT content FROM observations_local WHERE id = ?", (source_id,)
            ).fetchone()
            con.close()
            return (row[0], None, None, None, None, None, None) if row and row[0] else None
        if source == "decisions":
            con = _connect()
            row = con.execute(
                "SELECT decision, epistemic_status, mem_type, problem_class, validity_domain, "
                "structural_signature, canonical_id "
                "FROM decisions WHERE id = ?",
                (source_id,)
            ).fetchone()
            con.close()
            if not row:
                return None
            return (row["decision"], row["epistemic_status"], row["mem_type"],
                    row["problem_class"], row["validity_domain"], row["structural_signature"],
                    row["canonical_id"])
    except Exception:
        return None
    return None


def get_skeleton(source_id: object) -> Optional[str]:
    """Devuelve el ``skeleton`` (plantilla de código reutilizable) de un átomo por su id.

    Usado por la inyección de skeleton al build flow: cuando un átomo muy relevante surge en
    un prompt de construcción, su plantilla se inyecta como guía. None si el átomo no existe o
    no tiene skeleton. Read-only, una sola fila.
    """
    try:
        con = _connect()
        row = con.execute(
            "SELECT skeleton FROM decisions WHERE id = ?", (source_id,)
        ).fetchone()
        con.close()
        if row and row["skeleton"] and str(row["skeleton"]).strip():
            return str(row["skeleton"])
        return None
    except Exception:
        return None


def semantic_recall(query: str, client_id: Optional[str] = None,
                    limit: int = 5, min_similarity: float = 0.3) -> list[dict]:
    """Vector KNN recall over the sqlite-vec sidecar, hydrated with source text.

    Additive layer over claude-mem.db observations + sessions.db decisions. Returns []
    when the sidecar is unavailable/empty so FTS5 callers keep working unchanged.
    Pass client_id to scope results to one client (per-client isolation).
    """
    from . import diverse_recall, vector_store

    # Sobre-pedir candidatos para sobrevivir al filtro epistémico (se recorta a limit al final).
    k = max(diverse_recall.pool_size(limit), limit * 4)
    out: list[dict] = []
    for h in vector_store.search(query, client_id=client_id, k=k):
        if h["similarity"] < min_similarity:
            continue
        hyd = _hydrate(h["source"], h["source_id"])
        if not hyd:
            continue
        text, est, mtype, pclass, vdom, sig, can_id = hyd
        # Taxonomía (2026-06): nunca inyectar como guía lo muerto/log; queda queryable aparte.
        if mtype == "provenance" or est in ("refuted", "superseded"):
            continue
        # Dedup lógico (2026-06-29): excluir facts no-canónicos del recall semántico.
        if mtype == "fact" and can_id is not None and str(can_id) != "" and str(can_id) != h["source_id"]:
            continue
        # Etiquetar lo no-confirmado para que NO se haga pasar por verdad confirmada.
        if est == "provisional":
            text = "(sin verificar) " + text
        elif est == "open_question":
            text = "(pregunta abierta) " + text
        out.append({
            "source": h["source"],
            "source_id": h["source_id"],
            "client_id": h["client_id"],
            "similarity": h["similarity"],
            "text": text,
            "epistemic_status": est,
            "mem_type": mtype,
            "problem_class": pclass,
            "validity_domain": vdom,
            "structural_signature": sig,
        })
    if diverse_recall.enabled():  # opt-in reversible; off -> comportamiento identico al anterior
        return diverse_recall.reorder(query, out, limit)
    return out[:limit]


def save_gate_result(result: dict) -> None:
    """Save gate result to sessions.db with error handling.

    Args:
        result: Gate result dictionary with module, timestamp, status, steps, etc.
    """
    db = _connect()
    try:
        db.execute(
            """INSERT INTO gate_results
               (module_name, timestamp, status, details, e2e_prompt, session_ref)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                result["module"],
                result["timestamp"],
                result["status"],
                json.dumps({"steps": result["steps"], "status": result["status"]}),
                result.get("e2e_prompt", ""),
                result.get("session_ref", "")
            )
        )
        db.commit()
    finally:
        db.close()


def get_gate_results(module_name: str, limit: int = 10) -> list[dict]:
    """Query gate results for a module (newest first).

    Args:
        module_name: Name of the module to query
        limit: Maximum number of results to return

    Returns:
        List of gate result dictionaries
    """
    db = _connect()
    rows = db.execute(
        """SELECT module_name, timestamp, status, details, e2e_prompt
           FROM gate_results
           WHERE module_name = ?
           ORDER BY timestamp DESC
           LIMIT ?""",
        (module_name, limit)
    ).fetchall()
    db.close()
    return [dict(row) for row in rows]


def search_gate_results(query: str, limit: int = 10) -> list[dict]:
    """Search gate results via FTS5.

    Args:
        query: FTS5 search query
        limit: Maximum number of results to return

    Returns:
        List of matching gate results
    """
    db = _connect()
    fts_query = " OR ".join(query.split())
    try:
        rows = db.execute(
            """SELECT module_name, timestamp, status, details
               FROM gate_results
               WHERE rowid IN (SELECT rowid FROM gate_results_fts WHERE gate_results_fts MATCH ?)
               ORDER BY timestamp DESC
               LIMIT ?""",
            (fts_query, limit)
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]
    except Exception:
        db.close()
        return []


def init_modules_table() -> None:
    """Initialize modules_completed table and FTS5 index.

    Creates a table to track per-module validation state (PASS/FAIL)
    with quality metrics. Includes FTS5 virtual table for searching.
    """
    db = _connect()
    db.executescript("""
        CREATE TABLE IF NOT EXISTS modules_completed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module_name TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            quality_metrics TEXT,
            timestamp TEXT NOT NULL,
            session_ref TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS modules_completed_fts USING fts5(
            module_name, status, quality_metrics,
            content='modules_completed', content_rowid='rowid'
        );

        CREATE TRIGGER IF NOT EXISTS modules_completed_ai AFTER INSERT ON modules_completed BEGIN
            INSERT INTO modules_completed_fts(rowid, module_name, status, quality_metrics)
            VALUES (new.rowid, new.module_name, new.status, new.quality_metrics);
        END;
    """)
    db.close()


def save_module_result(
    module_name: str,
    status: str,
    quality_metrics: dict,
    session_ref: str | None = None,
) -> None:
    """Save or update module validation result to sessions.db.

    Args:
        module_name: Name of the module being validated
        status: Validation status ("PASS" or "FAIL")
        quality_metrics: Dict with {completeness_check, contract_validation, pattern_checks}
        session_ref: Optional reference to current session ID
    """
    db = _connect()
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    metrics_json = json.dumps(quality_metrics) if quality_metrics else "{}"

    try:
        db.execute(
            """INSERT OR REPLACE INTO modules_completed
               (module_name, status, quality_metrics, timestamp, session_ref)
               VALUES (?, ?, ?, ?, ?)""",
            (module_name, status, metrics_json, timestamp, session_ref),
        )
        db.commit()
    finally:
        db.close()


def get_module_results(module_name: str | None = None, limit: int = 10) -> list[dict]:
    """Query module validation results from sessions.db.

    Args:
        module_name: If specified, return all results for that module.
                     If None, return last {limit} completed modules
        limit: Maximum number of results to return

    Returns:
        List of module result dictionaries with all fields
    """
    db = _connect()
    try:
        if module_name:
            rows = db.execute(
                """SELECT module_name, status, quality_metrics, timestamp, session_ref
                   FROM modules_completed
                   WHERE module_name = ?
                   ORDER BY timestamp DESC""",
                (module_name,),
            ).fetchall()
        else:
            rows = db.execute(
                """SELECT module_name, status, quality_metrics, timestamp, session_ref
                   FROM modules_completed
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()

        results = []
        for row in rows:
            result_dict = dict(row)
            # Parse quality_metrics JSON if present
            if result_dict.get("quality_metrics"):
                try:
                    result_dict["quality_metrics"] = json.loads(result_dict["quality_metrics"])
                except (json.JSONDecodeError, TypeError):
                    result_dict["quality_metrics"] = {}
            return_value = {
                "module_name": result_dict["module_name"],
                "status": result_dict["status"],
                "quality_metrics": result_dict.get("quality_metrics", {}),
                "timestamp": result_dict["timestamp"],
                "session_ref": result_dict.get("session_ref"),
            }
            results.append(return_value)

        return results
    finally:
        db.close()


def store_design_decision(
    decision: str,
    rationale: str,
    evidence: str,
    domain: str = 'design_system',
    session_ref: str | None = None,
    locked: bool = True
) -> int | None:
    """
    Store a design decision in sessions.db as a locked decision.

    Args:
        decision: What was decided (e.g., "Primary color is #007AFF")
        rationale: Why this decision was made
        evidence: Where this decision is visible (mockup filename, etc.)
        domain: Category (default: 'design_system')
        session_ref: Reference (default: 'claude-design-{timestamp}')
        locked: Whether this decision is immutable (default: True)

    Returns:
        Row ID of inserted decision
    """
    db = _connect()

    if session_ref is None:
        session_ref = f"claude-design-{datetime.now(UTC).isoformat()}"

    try:
        cursor = db.execute("""
            INSERT INTO decisions (decision, rationale, domain, locked, session_ref, evidence)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (decision, rationale, domain, 1 if locked else 0, session_ref, evidence))

        db.commit()
        row_id = cursor.lastrowid
        return row_id
    finally:
        db.close()


def save_v15_state(key: str, value: dict) -> None:
    """Save V15 state to sessions.db with JSON serialization.

    Args:
        key: State key (e.g., 'hook_router_metrics', 'token_intelligence', 'agent_orchestrator')
        value: Dictionary to store (will be JSON-serialized)
    """
    db = _connect()
    try:
        serialized = json.dumps(value)
        db.execute(
            "INSERT OR REPLACE INTO v15_session_state (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, serialized),
        )
        db.commit()
    except Exception as e:
        # Write-path: fail-soft pero NO silencioso (lección del audit 2026-06-24). Si el estado
        # del agente no se persiste (DB locked, schema drift), debe quedar traza en stderr.
        print(f"⚠️ save_v15_state('{key}') falló: {type(e).__name__}: {e}", file=sys.stderr)
    finally:
        db.close()


def load_v15_state(key: str) -> dict:
    """Load V15 state from sessions.db with JSON deserialization.

    Args:
        key: State key to load

    Returns:
        Deserialized dictionary, or empty dict if not found or on error
    """
    db = _connect()
    try:
        row = db.execute(
            "SELECT value FROM v15_session_state WHERE key = ?",
            (key,),
        ).fetchone()
        if row:
            return json.loads(row[0])
    except (sqlite3.OperationalError, json.JSONDecodeError, TypeError):
        pass
    finally:
        db.close()
    return {}


# ---------------------------------------------------------------------------
# E3 — amplification_score (Batch E, 2026-07-06)
# ---------------------------------------------------------------------------

def _read_session_signals_from_log(session_id: str) -> dict[str, int]:
    """Read per-session capability and turn-count signals from the JSONL event log.

    Two event types now carry ``session_id`` in the log and are live:

    - ``capability_adopted``: emitted by ``capability_adoption.record_tool_use()``
      via the PostToolUse hook.  Counts hints that were actually used this session.
    - ``depth_inject``: emitted once per prompt turn by the depth-inject hook.
      Used as a proxy for ``total_turns`` (one event per user prompt).

    Live signals added by Batch O (2026-07-06):
    - ``f1_feedback``: now carries ``session_id`` (set from ARIS4U_SESSION_ID env by
      f1_feedback.record_feedback).  ``f1_total`` counts all f1_feedback events for
      the session; ``f1_useful`` counts those where ``useful`` is truthy.
    - ``phi_to_external_blocked`` / ``migration_lint_blocked``: now carry
      ``session_id`` (phi_guard._log_audit and migration_linter._emit_main_block_event).
      ``guard_blocks`` counts both event types.
    - ``model_routing_blocked``: emitted by ``~/.claude/hooks/model-routing-guard.py``
      (the external frontier hook). Previously this hook wrote only to
      ``~/.claude/logs/guard-blocks.jsonl`` (never read here) and without
      ``session_id``. Now it mirrors to this log with the ``session_id`` from the
      Claude Code hook payload, closing the attribution gap.

    Derives the log path from the ``ARIS4U_EVENTS_LOG`` env var (set by
    hooks) or falls back to ``<sessions_db_parent>/logs/v16.1-events.jsonl``.
    When ``SESSIONS_DB`` is patched in tests the fallback path will not exist,
    so the function returns all-zeros gracefully without touching real data.

    Args:
        session_id: Session identifier to filter events.

    Returns:
        dict with keys ``capabilities_adopted``, ``total_turns``, ``f1_useful``,
        ``f1_total``, ``guard_blocks`` (all int).
    """
    import os as _os

    _env_log = _os.environ.get("ARIS4U_EVENTS_LOG", "").strip()
    log_path: Path = (
        Path(_env_log)
        if _env_log
        else SESSIONS_DB.parent.parent / "logs" / "v16.1-events.jsonl"
    )
    counts: dict[str, int] = {
        "capabilities_adopted": 0,
        "total_turns": 0,
        "f1_useful": 0,
        "f1_total": 0,
        "guard_blocks": 0,
    }
    if not log_path.exists():
        return counts
    try:
        with log_path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                # Fast skip: lines without session_id cannot match.
                if not raw or '"session_id"' not in raw:
                    continue
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if evt.get("session_id") != session_id:
                    continue
                etype = evt.get("event", "")
                if etype == "capability_adopted":
                    counts["capabilities_adopted"] += 1
                elif etype == "depth_inject":
                    counts["total_turns"] += 1
                elif etype == "f1_feedback":
                    counts["f1_total"] += 1
                    if evt.get("useful"):
                        counts["f1_useful"] += 1
                elif etype in (
                    "phi_to_external_blocked",
                    "migration_lint_blocked",
                    "model_routing_blocked",
                ):
                    counts["guard_blocks"] += 1
    except OSError:
        pass
    return counts


def compute_amplification_score(session_id: str, db: Optional[sqlite3.Connection] = None) -> dict:
    """Compute the amplification_score for a session from available signals.

    Signals verified against real data (Batch F, 2026-07-06):

    LIVE (now populated):
    - recalls_useful / recalls_total: recall_feedback JOIN recall_events
      (session_id joinable via SQL).
    - capabilities_adopted: capability_adopted events in JSONL log filtered
      by session_id (emitted by PostToolUse / capability_adoption.py).
    - total_turns: depth_inject events in JSONL log filtered by session_id
      (one event per prompt turn — proxy for total interaction count).

    LIVE (added by Batch O, 2026-07-06):
    - f1_useful / f1_total: f1_feedback events now carry session_id
      (set from ARIS4U_SESSION_ID env by f1_feedback.record_feedback).
    - guard_blocks: phi_to_external_blocked (phi_guard) and
      migration_lint_blocked (migration_linter) now carry session_id.

    score = (recalls_useful + f1_useful + capabilities_adopted + guard_blocks)
            / max(recalls_total + f1_total, 1)

    Args:
        session_id: Session identifier (matches recall_events.session_id).
        db: Optional open connection (caller manages lifecycle). If None,
            opens and closes its own connection.

    Returns:
        Dict with all signal components, score, and signals_note.
    """
    close_after = db is None
    if db is None:
        db = _connect()
    recalls_total = recalls_useful = 0
    try:
        row = db.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(rf.useful), 0) AS useful
            FROM recall_events re
            LEFT JOIN recall_feedback rf ON rf.recall_id = re.recall_id
            WHERE re.session_id = ?
            """,
            (session_id,),
        ).fetchone()
        if row:
            recalls_total = row[0] or 0
            recalls_useful = int(row[1] or 0)
    except Exception:
        pass
    finally:
        if close_after:
            db.close()

    # Live signals from JSONL log (session_id-joinable as of Batch F + Batch O)
    log_signals = _read_session_signals_from_log(session_id)
    capabilities_adopted: int = log_signals["capabilities_adopted"]
    total_turns: int = log_signals["total_turns"]
    f1_useful: int = log_signals["f1_useful"]
    f1_total: int = log_signals["f1_total"]
    guard_blocks: int = log_signals["guard_blocks"]

    numerator = recalls_useful + f1_useful + capabilities_adopted + guard_blocks
    denominator = max(recalls_total + f1_total, 1)
    score = numerator / denominator

    # signals_note: always names all five signal areas for auditability
    note_parts = [
        (
            f"f1_labels: {f1_useful}/{f1_total} útiles (live)"
            if f1_total > 0
            else "f1_labels: 0 eventos esta sesión (live)"
        ),
        (
            f"capability_adopted: {capabilities_adopted} eventos (live)"
            if capabilities_adopted > 0
            else "capability_adopted: 0 eventos esta sesión (live)"
        ),
        (
            f"guard_blocks: {guard_blocks} bloqueos (live)"
            if guard_blocks > 0
            else "guard_blocks: 0 bloqueos esta sesión (live)"
        ),
    ]
    signals_note = "; ".join(note_parts)

    return {
        "session_id": session_id,
        "recalls_useful": recalls_useful,
        "recalls_total": recalls_total,
        "f1_useful": f1_useful,
        "f1_total": f1_total,
        "capabilities_adopted": capabilities_adopted,
        "guard_blocks": guard_blocks,
        "total_turns": total_turns,
        "score": round(score, 4),
        "signals_note": signals_note,
    }


def write_amplification_score(session_id: str) -> None:
    """Compute and persist the amplification_score for session_id at session close.

    Write-path: fail-soft but NOT silent — logs to stderr on error so the failure
    is detectable (lección del audit 2026-06-24: fail-open != fail-silencioso).

    Args:
        session_id: Session identifier used as the table primary key.
    """
    db = _connect()
    try:
        data = compute_amplification_score(session_id, db=db)
        db.execute(
            """
            INSERT OR REPLACE INTO amplification_scores
                (session_id, computed_at, recalls_useful, recalls_total,
                 f1_useful, f1_total, capabilities_adopted, guard_blocks,
                 total_turns, score, signals_note)
            VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                data["recalls_useful"],
                data["recalls_total"],
                data["f1_useful"],
                data["f1_total"],
                data["capabilities_adopted"],
                data["guard_blocks"],
                data["total_turns"],
                data["score"],
                data["signals_note"],
            ),
        )
        db.commit()
    except Exception as e:
        print(
            f"⚠️ write_amplification_score('{session_id}') falló: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
    finally:
        db.close()
