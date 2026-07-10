"""Handler SessionStart — portado de hooks/lab_session_init.sh (sin heredoc shell).

Cierra el gap de MCP voluntario (0% compliance histórico): al arrancar una sesión en
un proyecto-laboratorio, auto-inyecta recall de claude-mem.db (filtro de proyecto +
FTS5 keyword) vía additionalContext, sin que Claude tenga que invocar aris_search.
Side-effects preservados EXACTOS: puente de cliente para el demonio MCP (todo cwd) y
reset del presupuesto de tokens salvo source=="resume" (misma ventana de contexto).

Equivalencia verificada vs el .sh viejo en tests/dispatch/golden/session_start_*.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone, UTC
from pathlib import Path

from dispatch.contract import ARIS4U_ROOT, advise, passthrough
from dispatch.events._briefing import build_briefing

# Mapa default de proyectos-laboratorio: dir_name -> (claude-mem project, topic).
# Vacío por defecto — config ausente ⇒ comportamiento genérico (sin recall automático de lab).
# Para activar, configura "lab_projects" en ~/.aris4u/config.json:
#   {"lab_projects": [{"name": "mi-proyecto", "db_project": "mi-proyecto", "topic": "mi-proyecto"}]}
_DEFAULT_LAB_PROJECTS: list[tuple[str, str, str]] = []

# Alias para compatibilidad con código que importe LAB_PROJECTS directamente.
LAB_PROJECTS = _DEFAULT_LAB_PROJECTS


def _lab_projects() -> list[tuple[str, str, str]]:
    """Devuelve la lista de proyectos-lab desde config o los defaults exactos (fail-open).

    Config: ~/.aris4u/config.json campo "lab_projects" (lista de dicts con
    "name", "db_project" (opt), "topic" (opt)). Si el campo no existe o falla,
    devuelve _DEFAULT_LAB_PROJECTS sin cambios (comportamiento idéntico al actual).

    Returns:
        Lista de tuplas (dir_name, db_project, topic).
    """
    try:
        import json as _json

        cfg_path = os.environ.get("ARIS4U_CONFIG") or str(Path.home() / ".aris4u" / "config.json")
        p = Path(cfg_path)
        if p.is_file():
            cfg = _json.loads(p.read_text())
            projects = cfg.get("lab_projects")
            if projects:
                entries: list[tuple[str, str, str]] = []
                for item in projects:
                    if not isinstance(item, dict):
                        continue
                    # "path" puede ser "/home/x/projects/mi-proyecto/" → basename
                    name = item.get("name") or Path(item.get("path", "").rstrip("/")).name
                    db_project = item.get("db_project") or name
                    topic = item.get("topic") or name
                    if name:
                        entries.append((name, db_project, topic))
                if entries:
                    return entries
    except Exception:
        pass
    return list(_DEFAULT_LAB_PROJECTS)


def _detect_lab_project(cwd: str) -> tuple[str, str, str]:
    """Devuelve (dir, db_project, topic) si cwd cae dentro de un proyecto-lab; ('','','') si no.

    Match equivalente al case shell `*/"$dir"|*/"$dir"/*`: el segmento de directorio
    debe aparecer como componente del path (no como subcadena). Si ningún proyecto-lab
    matchea, intenta resolve_client_from_path para soportar el marcador .aris-client.

    Args:
        cwd: Directorio de trabajo actual.

    Returns:
        Tupla (dir_name, db_project, topic) o ('', '', '') si no se detecta proyecto.
    """
    parts = Path(cwd).parts
    for dir_name, db_project, topic in _lab_projects():
        if dir_name in parts:
            return dir_name, db_project, topic

    # Fallback: marcador .aris-client (cierra gap conocido)
    try:
        if str(ARIS4U_ROOT) not in sys.path:
            sys.path.insert(0, str(ARIS4U_ROOT))
        from engine.v16.session_manager import resolve_client_from_path  # noqa: PLC0415

        client = resolve_client_from_path(cwd)
        if client:
            return client, client, client
    except Exception:
        pass

    return "", "", ""


def _recall_by_project(conn: sqlite3.Connection, project_db: str) -> list[str]:
    """Path 1 del .sh: observaciones recientes filtradas por proyecto (LIMIT 8, DESC).

    Args:
        conn: Conexión a sessions.db (observations_local) con row_factory = sqlite3.Row.
        project_db: Nombre de proyecto en la columna `project`.

    Returns:
        Líneas formateadas `- [type] body` (cuerpo truncado a 200). Lista vacía
        ante cualquier error (fail-open) o si no hay filas con cuerpo.
    """
    snippets: list[str] = []
    try:
        # V18 Fase E: texto PROPIO (observations_local), no claude-mem.db muerta.
        cur = conn.execute(
            """
            SELECT type, content
            FROM observations_local
            WHERE project = ?
            ORDER BY rowid DESC
            LIMIT 8
            """,
            (project_db,),
        )
        for row in cur:
            body = (row["content"] or "").strip()
            if body:
                snippets.append(f"- [{row['type']}] {body[:200]}")
    except Exception:
        pass
    return snippets


def _recall_cross_project(conn: sqlite3.Connection, project_db: str, topic: str) -> list[str]:
    """Path 2 del .sh: FTS5 keyword cross-project (menciones en otros proyectos).

    Salta filas cuyo `project == project_db` (ya cubiertas en Path 1). El query
    ordena por rank con LIMIT 6 ANTES de filtrar same-project (semántica preservada).

    Args:
        conn: Conexión a sessions.db (observations_local) con row_factory = sqlite3.Row.
        project_db: Proyecto a excluir (ya cubierto por Path 1).
        topic: Término de búsqueda FTS5.

    Returns:
        Líneas `- [type from project] body` (cuerpo truncado a 200). Lista vacía
        ante cualquier error (fail-open).
    """
    snippets: list[str] = []
    try:
        # V18 Fase E: FTS5 propia (observations_local_fts, mapea por rowid).
        cur = conn.execute(
            """
            SELECT o.type, o.content, o.project
            FROM observations_local o
            JOIN observations_local_fts fts ON fts.rowid = o.rowid
            WHERE observations_local_fts MATCH ?
            ORDER BY rank
            LIMIT 6
            """,
            (topic,),
        )
        for row in cur:
            if row["project"] == project_db:
                continue  # ya cubierto en Path 1
            body = (row["content"] or "").strip()
            if body:
                snippets.append(f"- [{row['type']} from {row['project']}] {body[:200]}")
    except Exception:
        pass
    return snippets


def _build_recall(db_path: Path, project_db: str, topic: str) -> str:
    """Replica las dos rutas de consulta del .sh sobre claude-mem.db (project + FTS5).

    Args:
        db_path: Ruta a sessions.db (observations_local).
        project_db: Proyecto para el filtro de Path 1.
        topic: Término FTS5 para Path 2 (y etiqueta del header).

    Returns:
        Bloque markdown de auto-recall, o "" si la DB es inaccesible o no hay
        snippets (fail-open en todos los pasos).
    """
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except Exception:
        return ""

    snippets = _recall_by_project(conn, project_db)

    # Path 2 sólo si hay topic y aún no alcanzamos el umbral de 12.
    if topic and len(snippets) < 12:
        snippets.extend(_recall_cross_project(conn, project_db, topic))

    try:
        conn.close()
    except Exception:
        pass

    if not snippets:
        return ""

    out = [f"## Auto-recall: {topic} (last {len(snippets)} relevant observations)", ""]
    out.extend(snippets[:14])
    return "\n".join(out)


def _log_session_start_recall(
    recall_id: str,
    topic: str,
    session_id: str,
    n_snippets: int,
    injected: list[str],
) -> None:
    """Emite el evento auto_recall de session_start al JSONL de telemetría.

    Cierra el gap 0/126: el calificador de utilidad (tools/recall_usefulness.py)
    solo procesa eventos del JSONL. Sin este log, los recalls de session_start
    tenían session_id='' en SQL y nunca se juzgaban (source='session_start', 0/126).

    Usa source='session_start' e inyecta el topic como query (no hay prompt de usuario
    en ese momento). El calificador usa _full_session_text (toda la sesión) para estos
    eventos en vez de buscar un prompt-disparador específico.

    Args:
        recall_id: ID del recall ya registrado en SQL (mismo UUID hex[:12]).
        topic: Project topic (e.g. "client-a") — query proxy.
        session_id: ID de sesión de Claude Code, del evento SessionStart.
        n_snippets: Número de snippets inyectados.
        injected: Líneas «- [» del bloque recall (hasta 6).
    """
    try:
        override = os.environ.get("ARIS4U_EVENTS_LOG")
        lf = Path(override) if override else (ARIS4U_ROOT / "logs" / "v16.1-events.jsonl")
        if not lf.parent.exists():
            return
        with lf.open("a") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "event": "auto_recall",
                        "recall_id": recall_id,
                        "results": n_snippets,
                        "query": topic,
                        "client": topic,
                        "session_id": session_id,
                        "source": "session_start",
                        "injected": injected[:6],
                    }
                )
                + "\n"
            )
    except Exception:
        pass


def _register_recall_sql(
    project: str, n_snippets: int, db_path: "Path | None" = None, *, session_id: str = ""
) -> str:
    """Registra el recall de lab en recall_events (sessions.db). Fail-open, idempotente.

    Crea la tabla recall_events si no existe. Si el directorio padre de la DB no
    existe o la conexión falla, retorna silenciosamente (nunca rompe el arranque).

    Args:
        project: Nombre del proyecto (project_db), usado como etiqueta en SQL.
        n_snippets: Número de snippets inyectados (líneas «- [» del bloque recall).
        db_path: Ruta a sessions.db. Por defecto ARIS4U_ROOT/data/sessions.db.
        session_id: ID de sesión de Claude Code (para cruzar con transcripts en el
            calificador de utilidad). Antes era '' — fix del gap 0/126.

    Returns:
        recall_id generado (hex[:12]) para pasarlo a _log_session_start_recall.
    """
    recall_id = uuid.uuid4().hex[:12]
    try:
        _db: Path = db_path if db_path is not None else (ARIS4U_ROOT / "data" / "sessions.db")
        if not _db.parent.exists():
            return recall_id
        conn = sqlite3.connect(str(_db), timeout=3)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS recall_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "recall_id TEXT UNIQUE NOT NULL, "
            "ts TEXT NOT NULL, "
            "project TEXT DEFAULT '', "
            "n_snippets INTEGER DEFAULT 0, "
            "source TEXT DEFAULT 'session_start', "
            "query TEXT DEFAULT '', "
            "client TEXT DEFAULT '', "
            "session_id TEXT DEFAULT '')"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recall_events_ts " "ON recall_events(ts DESC)")
        ts = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO recall_events "
            "(recall_id, ts, project, n_snippets, source, client, session_id) "
            "VALUES (?, ?, ?, ?, 'session_start', ?, ?)",
            (recall_id, ts, project, n_snippets, project, session_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass
    return recall_id


def _writepath_warning() -> str:
    """Grito fail-loud si la última decisión escrita tiene >48h (write-path sospechoso).

    FREEZE ítem 1 (§7 del MASTER) · modo de fallo #3: el write-path se rompe en
    silencio (ya pasó 2×). El smoke roundtrip lo prueba activo a diario en el cron;
    esto te lo grita EN LA CARA al abrir sesión, no advisory que se ignora. Devuelve
    "" si la escritura es fresca o ante cualquier error (fail-open: nunca rompe el arranque).
    """
    try:
        db = sqlite3.connect(str(ARIS4U_ROOT / "data" / "sessions.db"), timeout=3)
        row = db.execute("SELECT MAX(created_at) FROM decisions").fetchone()
        db.close()
        if not row or not row[0]:
            return ""
        last = datetime.strptime(str(row[0])[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        age_h = (datetime.now(UTC) - last).total_seconds() / 3600
        if age_h > 48:
            return (
                f"⚠️ ARIS4U WRITE-PATH STALE — la última decisión se escribió hace {age_h:.0f}h (>48h).\n"
                "El roundtrip ingest→recall pudo romperse en silencio (ya ocurrió 2×: session_end "
                "descableado 7 días; ingest locked=1 → 0/1503). ANTES de confiar en la memoria de esta "
                "sesión: revisa logs/smoke_roundtrip.jsonl y corre `python3 tools/smoke_roundtrip.py`."
            )
    except Exception:
        return ""
    return ""


def _write_client_bridge(cwd: str) -> None:
    """Puente de cliente para el demonio MCP — corre SIEMPRE (cualquier cwd, no solo labs).

    Igual que el .sh: antes del early-exit. Fail-open: cualquier error se ignora.

    Args:
        cwd: Directorio de trabajo a pasar al script bridge.
    """
    try:
        subprocess.run(
            ["bash", str(ARIS4U_ROOT / "hooks" / "write_client_bridge.sh"), cwd],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _fired_from_test_suite() -> bool:
    """True si el proceso corre dentro de pytest.

    Los side-effects fire-and-forget de este módulo (tagger de claude-mem.db,
    rebuild del snapshot de capacidades) mutan ARCHIVOS/DBs REALES del host. Si un
    test ejercita handle() sin mockearlos, el subproceso hereda el entorno del test
    (p.ej. HOME falso) y CLOBBEREA el estado real — pasó 2026-07-01: la suite bajo
    HOME=/tmp/fake-ci-home reescribió capability_runtime_snapshot.json con
    mcp_tools vacío. Guard explícito > confiar en que todo test mockee.
    """
    return "PYTEST_CURRENT_TEST" in os.environ


def _tag_observations_async() -> None:
    """RETIRADO en V18 Fase E (paso 10, 2026-07-02): el tagger operaba sobre claude-mem.db
    (ahora archivada) y el mirror ``_mirror_to_claude_mem`` ya resuelve ``client_id`` EN LA
    ESCRITURA. El histórico sin client_id es NULL-por-diseño (sesiones genéricas, decisión
    Tramo 3 §9). El tool ``tools/tag_observations_client.py`` sigue disponible para uso
    manual sobre un backup. No-op permanente (se conserva el símbolo para el caller/tests).
    """
    return


def _refresh_capability_snapshot() -> None:
    """Regenera el snapshot de capacidades en background al arrancar la sesión.

    Ejecuta ``python3 -m tools.capability_inventory --rebuild`` como proceso
    desconectado (start_new_session=True) para que el snapshot refleje el toolkit
    actual del usuario sin bloquear el arranque. Idempotente.

    Fail-open: cualquier excepción se ignora silenciosamente (nunca rompe el
    arranque de sesión). Patrón idéntico a _tag_observations_async.
    No-op bajo pytest (_fired_from_test_suite) — el rebuild bajo HOME de test
    clobberea el snapshot real.
    """
    if _fired_from_test_suite():
        return
    try:
        subprocess.Popen(  # noqa: S603 — fire-and-forget, no esperamos resultado
            [
                sys.executable,
                "-m",
                "tools.capability_inventory",
                "--rebuild",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(ARIS4U_ROOT),
        )
    except Exception:
        pass


def _reset_token_budget(source: str) -> None:
    """Resetea el presupuesto de tokens salvo source=='resume' (misma ventana de contexto).

    Args:
        source: Origen de la sesión SessionStart ('startup', 'resume', …).
    """
    if source == "resume":
        return
    if str(ARIS4U_ROOT) not in sys.path:
        sys.path.insert(0, str(ARIS4U_ROOT))
    try:
        from engine.v16.token_utils import TokenIntelligence

        TokenIntelligence().reset_budget()
    except Exception:
        pass


def _emit_briefing_telemetry(total_bytes: int) -> None:
    """Emite evento session_briefing al ARIS4U_LOG_FILE si los env vars están presentes.

    Args:
        total_bytes: Tamaño en chars del additionalContext emitido.
    """
    if not (os.environ.get("ARIS4U_VALIDATION_LOG") and os.environ.get("ARIS4U_LOG_FILE")):
        return
    try:
        from dispatch.events._briefing import _db_memory  # noqa: PLC0415

        n_clients = len(_db_memory().get("by_client", []))
        with open(os.environ["ARIS4U_LOG_FILE"], "a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "event": "session_briefing",
                        "clients": n_clients,
                        "bytes": total_bytes,
                    }
                )
                + "\n"
            )
    except Exception:
        pass


def _emit_lab_session_telemetry(topic: str, cwd: str) -> None:
    """Emite el evento lab_session_init (idéntico al .sh) para analyze_validation_log.

    Args:
        topic: Tópico del proyecto-lab detectado.
        cwd: Directorio de trabajo (truncado a 200 chars en el log).
    """
    if not (os.environ.get("ARIS4U_VALIDATION_LOG") and os.environ.get("ARIS4U_LOG_FILE")):
        return
    try:
        with open(os.environ["ARIS4U_LOG_FILE"], "a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "event": "lab_session_init",
                        "project": topic,
                        "cwd": cwd[:200],
                    }
                )
                + "\n"
            )
    except Exception:
        pass


def _emit_or_passthrough(briefing: str, warning: str) -> None:
    """Sale emitiendo briefing + warning si los hay; si no, no-op silencioso (passthrough).

    Ruta fuera-de-lab / sin-recall: une sólo briefing y warning con doble salto de línea.

    Args:
        briefing: Bloque de self-briefing (posiblemente vacío).
        warning: Aviso de write-path stale (posiblemente vacío).
    """
    parts: list[str] = []
    if briefing:
        parts.append(briefing)
    if warning:
        parts.append(warning)
    if parts:
        context = "\n\n".join(parts)
        _emit_briefing_telemetry(len(context))
        advise(context, "SessionStart")
    passthrough()


def _prepend_prefix(context: str, briefing: str, warning: str) -> str:
    """Antepone briefing y/o warning al contexto de recall del lab (separador '---').

    Args:
        context: Bloque de recall del proyecto-lab.
        briefing: Bloque de self-briefing (posiblemente vacío).
        warning: Aviso de write-path stale (posiblemente vacío).

    Returns:
        El contexto con los prefijos aplicables antepuestos; sin cambios si no hay ninguno.
    """
    prefix_parts: list[str] = []
    if briefing:
        prefix_parts.append(briefing)
    if warning:
        prefix_parts.append(warning)
    if prefix_parts:
        return "\n\n---\n\n".join(prefix_parts) + "\n\n---\n\n" + context
    return context


def _onboarding_config_missing() -> bool:
    """True si ARIS4U está instalado pero sin configurar.

    Señal barata y definitiva: ``~/.aris4u/config.json`` ausente o esencialmente vacío
    (< 10 bytes cubre inexistente, ``{}`` o JSON roto). Se apaga sola en cuanto
    ``aris4u_init`` escribe una config real. Fail-open: cualquier excepción → False.

    Returns:
        True si falta configuración; False si ya existe o ante cualquier error.
    """
    try:
        cfg_path = os.environ.get("ARIS4U_CONFIG") or str(Path.home() / ".aris4u" / "config.json")
        p = Path(cfg_path)
        return (not p.is_file()) or p.stat().st_size < 10
    except Exception:
        return False


def _detect_onboarding_needed(source: str) -> str:
    """Directiva imperativa de auto-onboarding de primera vez, o "" si no aplica.

    ARIS4U puede instalarlo alguien que NO es desarrollador y solo quiere hablar en
    lenguaje natural con Claude. Si el sistema está instalado pero sin configurar, esto
    hace que Claude CONDUZCA el setup por sí mismo (config, env vars en settings.json con
    permiso, índice semántico, verificación) sin que el usuario escriba comandos ni pegue
    nada. Un hook no puede invocar una skill; por eso inyecta texto imperativo que el
    modelo ejecuta.

    Gates (todos fail-open): nunca en ``source=="resume"``; nunca bajo pytest
    (``PYTEST_CURRENT_TEST``, protege golden tests en local y CI); opt-out con
    ``ARIS4U_AUTO_ONBOARD=0``; solo si falta configuración. Se apaga sola al escribirse.

    Args:
        source: origen del SessionStart ("startup"/"resume"/...).

    Returns:
        El texto imperativo de onboarding, o "" si no debe dispararse.
    """
    if source == "resume":
        return ""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return ""
    if os.environ.get("ARIS4U_AUTO_ONBOARD", "1").strip().lower() in ("0", "false", "no"):
        return ""
    if not _onboarding_config_missing():
        return ""
    root = str(ARIS4U_ROOT)
    py = f"{root}/.venv312/bin/python3"
    return (
        "🔧 ARIS4U — PRIMERA VEZ (auto-onboarding)\n\n"
        "ARIS4U está instalado pero aún no configurado. **Quien instala puede NO ser "
        "desarrollador**: no le hagas escribir comandos ni pegar bloques. CONDUCE tú el "
        "setup ahora, de forma conversacional, y pide permiso solo para escribir archivos. "
        "Haz estos pasos y ve confirmando en lenguaje llano:\n\n"
        f"1. Genera su config: `{py} {root}/tools/aris4u_init.py --yes` (detecta hardware + "
        "proyectos → ~/.aris4u/config.json). Resume qué detectó.\n"
        "2. Activa la automatización recomendada: fusiona en su ~/.claude/settings.json (con "
        'backup, sin borrar lo suyo) este bloque "env": ENABLE_PROMPT_CACHING_1H=true, '
        "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS=15000, ARIS4U_DEPTH_PROTOCOL=1, "
        "ARIS4U_CONDUCTOR_ENFORCE=1, ARIS4U_HEALTHCARE=0, ARIS4U_ROUTER_SEM_THRESHOLD=0.70, "
        "ARIS4U_DIVERSE_RECALL=0. (El router y el protocolo de orquestación ya vienen ON por "
        "defecto.) TÚ editas el archivo; el usuario solo aprueba. Explícale en una línea qué "
        "gana (memoria + verificación de código + orquestación automática de su toolkit).\n"
        "   • Si el usuario NO es desarrollador y quiere la experiencia totalmente hands-off "
        "('yo hablo, ustedes ejecutan'), añade también ARIS4U_AUTOPILOT=1: cuando su petición "
        "corresponda a una capacidad conocida, la EJECUTAS en vez de solo sugerirla. Si es "
        "desarrollador, déjalo en 0 (sugerencias, sin ejecución automática).\n"
        f"3. Enciende el router semántico: `{py} {root}/tools/capability_semantic.py --build` "
        "(construye el índice de capacidades; sin él, 'hablo → capacidad correcta' queda a "
        "medias). Si no hay Ollama, dilo y sigue: degrada limpio a búsqueda por texto.\n"
        "4. Verifica con la skill /aris-status y resume lo que quedó activo.\n"
        "5. Cierra diciéndole: a partir de ahora **solo describe lo que necesitas en lenguaje "
        "natural** — ARIS4U y yo reconocemos la intención y usamos las herramientas correctas "
        "por ti, de principio a fin. No hace falta que aprendas comandos.\n\n"
        "Si el usuario dice que es desarrollador y prefiere hacerlo a mano, respétalo y solo "
        "muéstrale los pasos. Este aviso desaparece solo al escribirse la config. Opt-out de "
        "sesión: ARIS4U_AUTO_ONBOARD=0."
    )


def handle(event_name: str, inp: dict) -> None:
    cwd = inp.get("cwd") or inp.get("working_directory") or os.getcwd()
    source = inp.get("source", "")

    # Auto-onboarding de primera vez: si ARIS4U está instalado pero sin configurar,
    # inyecta una directiva imperativa para que Claude conduzca el setup solo (el que
    # instala puede no ser desarrollador). Early-exit vía advise(). Fail-open total.
    onboarding = _detect_onboarding_needed(source)
    if onboarding:
        advise(onboarding, "SessionStart")  # sys.exit(0) — no retorna

    _write_client_bridge(cwd)

    # Captura en origen del scope por-cliente: etiqueta observations recientes que
    # claude-mem dejó con client_id NULL. Fire-and-forget, idempotente.
    _tag_observations_async()

    # Inventario universal: regenera el snapshot de capacidades en background para
    # que el router siempre refleje el toolkit real del usuario. Fire-and-forget.
    _refresh_capability_snapshot()

    # Reset del presupuesto de tokens salvo "resume" (misma ventana). Corre para TODA sesión.
    _reset_token_budget(source)

    # Self-briefing automático: solo en arranque nuevo (no en resume).
    # build_briefing es fail-open total: cualquier excepción devuelve "".
    briefing = build_briefing(source) if source != "resume" else ""

    # FREEZE ítem 1 — fail-loud del write-path: independiente de lab/recall. Si la última
    # escritura tiene >48h hay que gritarlo SIEMPRE, aunque la sesión no sea de lab.
    warning = _writepath_warning()

    # Detectar proyecto-lab desde cwd; fuera de lab → solo el warning (si lo hay).
    project_dir, project_db, topic = _detect_lab_project(cwd)
    if not topic:
        _emit_or_passthrough(briefing, warning)

    # V18 Fase E: recall lab desde el texto PROPIO (sessions.db/observations_local).
    from engine.v16.config import SESSIONS_DB as _SESSIONS_DB

    db_path = _SESSIONS_DB
    if not db_path.exists():
        _emit_or_passthrough(briefing, warning)

    # Move #2 — session_start recall gateado por ARIS4U_SESSION_START_RECALL (Fable-Gate 2026-07-05).
    # Auditoría: 248 eventos source='session_start', solo 0.8% marcados útiles → impuesto
    # de contexto puro. Por defecto OFF (deshabilitado). Para rollback o experimentos:
    #   export ARIS4U_SESSION_START_RECALL=1
    # El BRIEFING (build_briefing) NO se toca — sigue activo siempre.
    # El user_prompt recall (source='user_prompt', 48% útil) NO se toca.
    # Move #1 (tagging client_id): sin recall no hay evento que taggear → intacto (no-op limpio).
    # Fail-open: si os.environ falla por cualquier razón, el gate aplica (recae a OFF).
    if os.environ.get("ARIS4U_SESSION_START_RECALL", "0").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        _emit_or_passthrough(briefing, warning)

    recall = _build_recall(db_path, project_db, topic)
    if not recall:
        _emit_or_passthrough(briefing, warning)

    # Registrar recall en SQL (FREEZE §7 — recall_events queryable por métricas).
    # Solo llega aquí si recall es no-vacío (_emit_or_passthrough llama sys.exit).
    n_snippets = sum(1 for ln in recall.splitlines() if ln.startswith("- ["))
    injected_lines = [ln for ln in recall.splitlines() if ln.startswith("- [")]
    # session_id del payload del evento — necesario para cruzar con transcripts
    # en el calificador de utilidad. Fix del gap 0/126 (antes siempre '').
    session_id_val = inp.get("session_id", "")
    recall_id = _register_recall_sql(project_db, n_snippets, session_id=session_id_val)
    _log_session_start_recall(recall_id, topic, session_id_val, n_snippets, injected_lines)

    context = (
        f"{recall}\n\n"
        "---\n"
        f"ARIS4U auto-injected: {topic} project recall.\n\n"
        f"This context loaded automatically because cwd is in **{project_dir}** (a lab project).\n"
        "You did NOT invoke aris_search manually — that voluntary call has 0% historical compliance\n"
        "(see feedback_v13_enforcement_gap.md). lab_session_init hook closes that gap.\n\n"
        "For deeper queries during this session:\n"
        '- aris_search("specific topic") — FTS5 + semantic recall\n'
        "- aris_dialectic(task) — 3-agent adversarial review for critical code\n"
        "- aris_ingest(decision, rationale) — capture LOCKED decisions\n"
    )

    _emit_lab_session_telemetry(topic, cwd)

    # Anteponer briefing y/o warning al contexto de recall del lab.
    context = _prepend_prefix(context, briefing, warning)

    _emit_briefing_telemetry(len(context))
    advise(context, "SessionStart")
