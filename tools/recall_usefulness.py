#!/usr/bin/env python3
"""Calificador automático de utilidad de recalls — "utilidad implícita, costo cero".

Enciende el termómetro del freeze (la métrica `recalls útiles/semana`, ítem 0 del plan en
``architecture/ARIS4U_MASTER.md`` §7) sin depender del marcado humano manual
(``tools/freeze_report.py --mark``, que nadie corre → la tabla quedaba en 0 filas).

SEÑAL (honesta, sin LLM, sin costo de API): un ``auto_recall`` es ÚTIL si la memoria
inyectó términos DISTINTIVOS que NO estaban en el prompt del usuario pero SÍ aparecen en la
acción inmediata de Claude (texto de respuesta + inputs de herramientas). Es decir, mide la
contribución MARGINAL del recall: que haya aportado algo nuevo y que eso se haya usado.

FUENTES (ambas ya existen, no se reinventa nada):
  - ``logs/v16.1-events.jsonl``: eventos ``auto_recall`` con
    ``{recall_id, query, injected, session_id, ts}`` (``injected`` y ``session_id`` los
    añade el hook ``hooks/dispatch/events/user_prompt_submit.py`` — instrumentación forward).
  - ``~/.claude/projects/*/<session_id>.jsonl``: el transcript de Claude Code, de donde se
    lee SOLO la respuesta del asistente (estructura estable), nunca el bloque inyectado
    (ese se toma del log, fuente autoritativa).

ESCRIBE ``data/sessions.db`` tabla ``recall_feedback``, extendida con ``method`` ('implicit'
| 'manual'), ``score`` y ``detail``. Idempotente: NUNCA pisa una marca ``method='manual'``
(el juicio humano gana); re-evalúa solo las suyas. ``freeze_report.py`` lee la misma tabla
sin cambios → la métrica se enciende sola.

Uso:
    python3 tools/recall_usefulness.py --dry-run [--days N]   # qué marcaría (no escribe)
    python3 tools/recall_usefulness.py --apply   [--days N]   # persiste las marcas implícitas
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

# --- Parámetros de la heurística (tunables; "lo corriges si es necesario") ------------
MIN_DISTINCTIVE_LEN = 6  # longitud mínima para que un token cuente como "distintivo"
MIN_TERMS = 2            # nº de términos distintivos nuevos usados para declarar ÚTIL
MIN_TOKEN_LEN = 3        # longitud mínima para tokenizar

# Stopwords ES+EN (alto valor) + conectores/adverbios largos comunes (evitan falsos
# positivos por coincidencia de palabras genéricas) + ruido de prompts/herramientas.
_STOP = {
    # español — funcionales
    "que", "con", "los", "las", "del", "una", "uno", "por", "para", "como", "más",
    "pero", "sus", "este", "esta", "esto", "esos", "esas", "son", "fue", "han", "hay",
    "sin", "sobre", "entre", "cuando", "donde", "porque", "todo", "toda", "todos",
    "cada", "ese", "esa", "muy", "ser", "está", "estan", "están", "tiene", "hace",
    "puede", "debe", "solo", "sólo", "ya", "lo", "le", "se", "su", "tu", "mi", "de",
    "en", "el", "la", "un", "y", "o", "a", "al", "no", "si", "sí", "es",
    # español — conectores/adverbios largos comunes (≥6, inflarían "distintivo")
    "nunca", "siempre", "primero", "segundo", "tambien", "también", "entonces",
    "mientras", "aunque", "despues", "después", "antes", "ahora", "luego", "sino",
    "mismo", "misma", "mismos", "mismas", "hacia", "desde", "hasta", "segun", "según",
    "cosa", "cosas", "algo", "alguien", "nadie", "poco", "mucho", "mucha", "muchos",
    "muchas", "tanto", "tanta", "bien", "mejor", "grande", "nuevo", "nueva", "otra",
    "otro", "otros", "otras", "aqui", "aquí", "alli", "allí", "ademas", "además",
    "manera", "forma", "parte", "vez", "veces", "hacer", "hecho", "decir", "dice",
    # inglés
    "the", "and", "for", "are", "was", "with", "this", "that", "from", "you", "your",
    "has", "have", "not", "but", "all", "any", "can", "will", "what", "when", "which",
    "out", "use", "via", "per", "its", "into", "than", "then", "they", "them", "about",
    "over", "under", "again", "just", "very", "much", "more", "most", "some", "such",
    "only", "also", "because", "while", "where", "there", "their", "would", "could",
    "should", "being", "been", "does", "done", "make", "made", "need", "want", "like",
    "know", "time", "work", "good", "best", "thing", "things", "here",
    # ruido de prompt/herramientas
    "http", "https", "true", "false", "null", "none", "text", "json", "type", "name",
    "value", "file", "line", "self", "def", "str", "int", "dict", "list", "return",
}


def _root() -> Path:
    """Resuelve ARIS4U_ROOT (env) o la raíz del repo desde este archivo."""
    env = os.environ.get("ARIS4U_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def _projects_dir() -> Path:
    """Directorio raíz de los transcripts de Claude Code (~/.claude/projects)."""
    env = os.environ.get("ARIS4U_TRANSCRIPTS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "projects"


# --- Tokenización / distintividad -----------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_./-]*", re.IGNORECASE)
# Andamiaje del bloque inyectado: '~0.65', '[source#id]', '(domain)', viñetas y flechas.
_SCAFFOLD_RE = re.compile(r"~\d+\.\d+|\[[^\]]*\]|\([^)]*\)|->|[·!~]")


def is_identifier(tok: str) -> bool:
    """True si el token parece un identificador de código/ruta (señal fuerte de uso).

    Args:
        tok: Token ya en minúsculas.

    Returns:
        True si contiene ``_ . / -`` o un dígito y tiene longitud >= 4.
    """
    if len(tok) < 4:
        return False
    return bool(re.search(r"[_./-]", tok)) or bool(re.search(r"\d", tok))


def tokenize(text: str, *, strip_scaffold: bool = False) -> set[str]:
    """Extrae el conjunto de tokens significativos (minúsculas, sin stopwords).

    Args:
        text: Texto crudo.
        strip_scaffold: Si True, elimina primero el andamiaje del bloque inyectado
            (scores de similitud, ``[source#id]``, ``(domain)``, viñetas).

    Returns:
        Conjunto de tokens en minúsculas, longitud >= MIN_TOKEN_LEN, no stopwords.
    """
    if not text:
        return set()
    low = text.lower()
    if strip_scaffold:
        low = _SCAFFOLD_RE.sub(" ", low)
    out: set[str] = set()
    for m in _TOKEN_RE.finditer(low):
        tok = m.group(0).strip("._/-")
        if len(tok) < MIN_TOKEN_LEN or tok in _STOP:
            continue
        out.add(tok)
    return out


def judge(injected: list[str], query: str, response: str) -> tuple[bool, float, list[str]]:
    """Decide si un recall fue ÚTIL por contribución marginal usada.

    útil = la memoria aportó términos distintivos AUSENTES del prompt que SÍ aparecen en la
    acción siguiente de Claude. Identificadores usados pesan como señal fuerte.

    Args:
        injected: Líneas inyectadas (el bloque '🧠 RECALL'), tal como se loguearon.
        query: Prompt original del usuario (recortado).
        response: Texto de la respuesta de Claude (asistente + inputs de herramientas).

    Returns:
        Tupla ``(useful, score, matched)``: veredicto, puntaje y términos casados (muestra).
    """
    inj = set()
    for line in injected:
        inj |= tokenize(line, strip_scaffold=True)
    q_terms = tokenize(query)
    resp_terms = tokenize(response)

    novel = inj - q_terms                       # lo que el recall AÑADIÓ sobre el prompt
    used = novel & resp_terms                   # lo añadido que Claude USÓ
    used_ids = {t for t in used if is_identifier(t)}
    used_dist = {t for t in used if len(t) >= MIN_DISTINCTIVE_LEN}

    useful = bool(used_ids) or len(used_dist) >= MIN_TERMS
    score = float(len(used_dist) + 2 * len(used_ids))
    matched = sorted(used_ids) + sorted(used_dist - used_ids)
    return useful, score, matched[:12]


# --- Carga de eventos y transcripts ---------------------------------------------------

def _parse_event_line(line: str, since: datetime | None) -> dict | None:
    """Parsea una línea JSONL a un evento auto_recall en ventana, o None si no aplica.

    Args:
        line: Línea cruda del log.
        since: Límite inferior temporal (UTC, aware) o None.

    Returns:
        El evento (con ``_ts``) si es un auto_recall válido en ventana; si no, None.
    """
    line = line.strip()
    if not line or '"auto_recall"' not in line:
        return None
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        return None
    if ev.get("event") != "auto_recall":
        return None
    try:
        ts = datetime.fromisoformat(ev.get("ts", ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None
    if since is not None and ts < since:
        return None
    ev["_ts"] = ts
    return ev


def load_recall_events(log_path: Path, since: datetime | None) -> list[dict]:
    """Lee eventos ``auto_recall`` del JSONL dentro de la ventana [since, now].

    Args:
        log_path: Ruta al log enriquecido ``logs/v16.1-events.jsonl``.
        since: Límite inferior temporal (UTC, aware) o None para no filtrar.

    Returns:
        Lista de eventos auto_recall (dicts) con campo ``_ts`` añadido.
    """
    out: list[dict] = []
    if not log_path.exists():
        return out
    with log_path.open() as fh:
        for line in fh:
            ev = _parse_event_line(line, since)
            if ev is not None:
                out.append(ev)
    return out


def _entry_text(entry: dict) -> str:
    """Texto plano del bloque de mensaje de un entry de transcript (no tool_result)."""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
                parts.append(str(p.get("text", "")))
            elif p.get("type") == "tool_use":
                parts.append(json.dumps(p.get("input", {}), default=str))
        return "\n".join(parts)
    return ""


def _is_user_prompt(entry: dict) -> bool:
    """True si el entry es un prompt REAL del usuario (no un tool_result)."""
    if entry.get("type") != "user" or "toolUseResult" in entry:
        return False
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(isinstance(p, dict) and p.get("type") == "text" for p in content)
    return False


def find_transcript(session_id: str, projects_dir: Path) -> Path | None:
    """Localiza ``<session_id>.jsonl`` bajo cualquier proyecto (session_id es único)."""
    if not session_id:
        return None
    hits = list(projects_dir.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _norm(s: str) -> str:
    """Normaliza para emparejar: minúsculas + colapso de espacios."""
    return re.sub(r"\s+", " ", s.lower()).strip()


def _load_jsonl_rows(path: Path) -> list[dict]:
    """Carga un ``.jsonl`` a una lista de dicts (líneas inválidas se omiten)."""
    rows: list[dict] = []
    try:
        with path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return rows


def _find_prompt_index(rows: list[dict], target: str) -> int | None:
    """Índice del primer prompt real del usuario cuyo texto empieza por ``target``."""
    for i, e in enumerate(rows):
        if _is_user_prompt(e) and _norm(_entry_text(e)).startswith(target):
            return i
    return None


def _collect_assistant_after(rows: list[dict], start: int) -> str:
    """Concatena texto del asistente desde ``start+1`` hasta el siguiente prompt usuario."""
    chunks: list[str] = []
    for e in rows[start + 1:]:
        if _is_user_prompt(e):
            break
        if e.get("type") == "assistant":
            chunks.append(_entry_text(e))
    return "\n".join(c for c in chunks if c)


def _full_session_text(transcript: Path) -> str:
    """Concatena todo el texto del asistente en el transcript completo.

    Para recalls de ``source='session_start'`` no existe un prompt-disparador
    específico (el recall se inyecta antes de cualquier mensaje del usuario).
    Se evalúa si los términos inyectados aparecen en CUALQUIER turno del asistente
    en la sesión — señal de que el briefing fue efectivamente usado.

    Args:
        transcript: Ruta al ``.jsonl`` del transcript de la sesión.

    Returns:
        Texto concatenado de todos los turnos del asistente, o "" si no hay ninguno.
    """
    rows = _load_jsonl_rows(transcript)
    chunks: list[str] = []
    for e in rows:
        if e.get("type") == "assistant":
            chunks.append(_entry_text(e))
    return "\n".join(c for c in chunks if c)


def extract_response(transcript: Path, query: str) -> str | None:
    """Extrae la respuesta de Claude que sigue al prompt que disparó el recall.

    Empareja el prompt del usuario por prefijo normalizado y concatena el texto del
    asistente + inputs de herramientas hasta el siguiente prompt real del usuario.

    Args:
        transcript: Ruta al ``.jsonl`` del transcript de la sesión.
        query: ``query`` del evento (recortado a ~100 chars por el hook).

    Returns:
        Texto de la respuesta, o None si no se encontró el prompt disparador.
    """
    rows = _load_jsonl_rows(transcript)
    if not rows:
        return None
    target = _norm(query)[:60]
    if not target:
        return None
    start = _find_prompt_index(rows, target)
    if start is None:
        return None
    return _collect_assistant_after(rows, start)


# --- Persistencia ---------------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crea/extiende ``recall_feedback`` con method/score/detail (idempotente)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recall_feedback ("
        "recall_id TEXT PRIMARY KEY, useful INTEGER NOT NULL, marked_at TEXT NOT NULL)"
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recall_feedback)").fetchall()}
    if "method" not in cols:
        conn.execute("ALTER TABLE recall_feedback ADD COLUMN method TEXT DEFAULT 'manual'")
    if "score" not in cols:
        conn.execute("ALTER TABLE recall_feedback ADD COLUMN score REAL")
    if "detail" not in cols:
        conn.execute("ALTER TABLE recall_feedback ADD COLUMN detail TEXT")
    conn.commit()


def ensure_recall_events_schema(conn: sqlite3.Connection) -> None:
    """Crea recall_events e índice si no existen (idempotente).

    recall_events es la tabla SQL queryable que reemplaza el JSONL como fuente
    de métricas de recall para el freeze (§7 ARIS4U_MASTER). Contiene un registro
    por cada recall emitido, independientemente de si fue útil o no.

    Args:
        conn: Conexión SQLite abierta a sessions.db.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recall_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "recall_id TEXT UNIQUE NOT NULL, "
        "ts TEXT NOT NULL, "
        "project TEXT DEFAULT '', "
        "n_snippets INTEGER DEFAULT 0, "
        "source TEXT DEFAULT 'user_prompt', "
        "query TEXT DEFAULT '', "
        "client TEXT DEFAULT '', "
        "session_id TEXT DEFAULT '')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_recall_events_ts ON recall_events(ts DESC)"
    )
    conn.commit()


def sync_jsonl_to_sql(
    conn: sqlite3.Connection, log_path: Path, since: "datetime | None"
) -> int:
    """Importa eventos auto_recall del JSONL a recall_events (idempotente por recall_id).

    Lee los eventos del JSONL (emitidos por user_prompt_submit) e inserta los que
    no existan aún. Usa INSERT OR IGNORE para no pisar registros existentes.
    Llama a ensure_recall_events_schema antes de insertar.

    Args:
        conn: Conexión SQLite abierta a sessions.db.
        log_path: Ruta al log JSONL (logs/v16.1-events.jsonl).
        since: Límite inferior temporal (UTC, aware) o None para no filtrar.

    Returns:
        Número de filas nuevamente insertadas.
    """
    ensure_recall_events_schema(conn)
    events = load_recall_events(log_path, since)
    inserted = 0
    for ev in events:
        rid = ev.get("recall_id", "")
        if not rid:
            continue
        ts = ev.get("ts", "")
        n = ev.get("results") or ev.get("n_snippets") or len(ev.get("injected") or [])
        query = (ev.get("query") or "")[:200]
        client = ev.get("client", "")
        session_id = ev.get("session_id", "")
        # Preservar source del evento (ej. 'session_start') en vez de hardcodear
        # 'user_prompt'. Fix del gap 0/126: los eventos de session_start ahora se
        # escriben al JSONL con source='session_start'; deben llegar así a SQL.
        source = (ev.get("source") or "user_prompt")[:50]
        cur = conn.execute(
            "INSERT OR IGNORE INTO recall_events "
            "(recall_id, ts, project, n_snippets, source, query, client, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, ts, client, n, source, query, client, session_id),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def stats_from_sql(conn: sqlite3.Connection, days: int = 7) -> list[dict]:
    """Calcula recalls/semana y % útiles desde SQL (recall_events JOIN recall_feedback).

    Esta es la métrica del freeze (ARIS4U §7): cuántos recalls emitió el sistema y
    qué fracción fue evaluada como útil por recall_usefulness o el usuario. Computable
    por SQL sin depender del JSONL ni de transcripts de sesión.

    Args:
        conn: Conexión SQLite abierta a sessions.db.
        days: Ventana en días hacia atrás desde ahora (default 7).

    Returns:
        Lista de dicts ``{week, source, total, useful, pct_useful, recalls_per_week}``
        ordenados por semana DESC. Semana en formato YYYY-WW (ISO week number).
        Filas con total=0 no se incluyen.
    """
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        rows = conn.execute(
            """
            SELECT
                strftime('%Y-%W', substr(re.ts, 1, 10)) AS week,
                re.source,
                COUNT(*) AS total,
                SUM(CASE WHEN rf.useful = 1 THEN 1 ELSE 0 END) AS useful
            FROM recall_events re
            LEFT JOIN recall_feedback rf ON rf.recall_id = re.recall_id
            WHERE re.ts >= ?
            GROUP BY week, re.source
            ORDER BY week DESC, re.source
            """,
            (since,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    results: list[dict] = []
    weeks_seen: dict[str, int] = {}
    for week, source, total, useful in rows:
        weeks_seen[week] = weeks_seen.get(week, 0) + (total or 0)
        useful = useful or 0
        pct = round(100.0 * useful / total, 1) if total else 0.0
        results.append(
            {
                "week": week,
                "source": source,
                "total": total or 0,
                "useful": useful,
                "pct_useful": pct,
                "recalls_per_week": total or 0,  # dentro de la ventana days
            }
        )
    return results


def _print_stats_report(stats: list[dict], days: int, synced: int) -> None:
    """Imprime el reporte SQL de recalls/semana y % útiles.

    Args:
        stats: Lista de resultados de stats_from_sql.
        days: Ventana en días usada.
        synced: Filas JSONL recién sincronizadas a SQL.
    """
    print(f"\n=== RECALLS/SEMANA (SQL) · últimos {days} días ===")
    if synced:
        print(f"  (sincronizados desde JSONL: {synced} eventos nuevos)")
    if not stats:
        print("  Sin datos. Ejecuta --sync o --apply para poblar recall_events.")
        return
    print(f"\n{'Semana':10s} {'Source':14s} {'Total':6s} {'Útiles':7s} {'%':6s}")
    print("-" * 48)
    for r in stats:
        print(
            f"  {r['week']:8s}  {r['source']:12s}  {r['total']:4d}  "
            f"{r['useful']:5d}  {r['pct_useful']:5.1f}%"
        )
    total_all = sum(r["total"] for r in stats)
    useful_all = sum(r["useful"] for r in stats)
    weeks_count = max(1.0, days / 7)
    pct_all = round(100.0 * useful_all / total_all, 1) if total_all else 0.0
    print("-" * 48)
    print(
        f"  TOTAL: {total_all} recalls · {useful_all} útiles ({pct_all}%) "
        f"· {total_all / weeks_count:.1f} recalls/semana"
    )
    print(
        "  → Meta freeze: ≥3 útiles/semana sostenido 2 sem; "
        "<1 = re-diagnosticar recall"
    )


def upsert_implicit(
    conn: sqlite3.Connection, recall_id: str, useful: bool, score: float, detail: str
) -> bool:
    """Inserta/actualiza una marca implícita SIN pisar una marca manual.

    Returns:
        True si se escribió (insertó/actualizó); False si había una marca manual.
    """
    now = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO recall_feedback (recall_id, useful, marked_at, method, score, detail) "
        "VALUES (?,?,?,'implicit',?,?) "
        "ON CONFLICT(recall_id) DO UPDATE SET "
        "useful=excluded.useful, marked_at=excluded.marked_at, method='implicit', "
        "score=excluded.score, detail=excluded.detail "
        "WHERE recall_feedback.method != 'manual'",
        (recall_id, 1 if useful else 0, now, score, detail),
    )
    conn.commit()
    return cur.rowcount > 0


# --- Reporte semanal ------------------------------------------------------------------

def _weekly_stats(
    conn: sqlite3.Connection,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Calcula métricas de esta semana y la semana anterior desde SQL.

    Args:
        conn: Conexión SQLite abierta a sessions.db.

    Returns:
        Tupla ``(this_week, last_week)`` donde cada dict mapea
        ``source → {total, useful, pct}``.
    """
    now = datetime.now(UTC)
    w1_start = (now - timedelta(days=7)).isoformat()
    w0_start = (now - timedelta(days=14)).isoformat()

    def _query(from_ts: str, to_ts: str) -> dict[str, dict]:
        try:
            rows = conn.execute(
                """
                SELECT re.source,
                       COUNT(*) AS total,
                       SUM(CASE WHEN rf.useful = 1 THEN 1 ELSE 0 END) AS useful
                FROM recall_events re
                LEFT JOIN recall_feedback rf ON rf.recall_id = re.recall_id
                WHERE re.ts >= ? AND re.ts < ?
                GROUP BY re.source
                """,
                (from_ts, to_ts),
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        result: dict[str, dict] = {}
        for source, total, useful in rows:
            useful = useful or 0
            pct = round(100.0 * useful / total, 1) if total else 0.0
            result[source] = {"total": total or 0, "useful": useful, "pct": pct}
        return result

    return _query(w1_start, now.isoformat()), _query(w0_start, w1_start)


def _format_weekly_report(
    this_week: dict[str, dict],
    last_week: dict[str, dict],
    generated_at: "datetime | None" = None,
) -> str:
    """Formatea el reporte semanal de amplificación como texto plano legible.

    Args:
        this_week: Métricas de esta semana por source.
        last_week: Métricas de la semana anterior por source.
        generated_at: Datetime de generación (default = ahora UTC).

    Returns:
        Bloque de texto multilínea listo para imprimir y/o loguear.
    """
    ts = (generated_at or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M UTC")

    def _section(week_data: dict[str, dict], label: str) -> list[str]:
        out = [f"{label}:"]
        total_all = sum(v["total"] for v in week_data.values())
        useful_all = sum(v["useful"] for v in week_data.values())
        for source, stats in sorted(week_data.items()):
            out.append(
                f"  {source:<16} {stats['total']:4d} recalls  "
                f"{stats['useful']:3d} útiles  ({stats['pct']:.1f}%)"
            )
        pct_all = round(100.0 * useful_all / total_all, 1) if total_all else 0.0
        out.append(
            f"  {'TOTAL':<16} {total_all:4d} recalls  "
            f"{useful_all:3d} útiles  ({pct_all:.1f}%)"
        )
        return out

    this_total_useful = sum(v["useful"] for v in this_week.values())
    last_total_useful = sum(v["useful"] for v in last_week.values())
    trend = this_total_useful - last_total_useful
    trend_str = f"+{trend}" if trend > 0 else str(trend)

    gate_met = this_total_useful >= 3 and last_total_useful >= 3
    if gate_met:
        gate_line = "GATE TRAMO 4: MET — >=3 utiles/semana sostenido 2 sem"
    else:
        missing: list[str] = []
        if this_total_useful < 3:
            missing.append(f"esta sem {this_total_useful}/3")
        if last_total_useful < 3:
            missing.append(f"sem anterior {last_total_useful}/3")
        gate_line = f"GATE TRAMO 4: NO MET — {', '.join(missing)}"

    lines: list[str] = [
        f"=== ARIS4U REPORTE SEMANAL DE AMPLIFICACION — {ts} ===",
        "",
        *_section(this_week or {}, "Esta semana"),
        "",
        *_section(last_week or {}, "Semana anterior"),
        "",
        f"Tendencia utiles: {last_total_useful} -> {this_total_useful} ({trend_str})",
        gate_line,
        "=================================================================",
    ]
    return "\n".join(lines)


def _append_to_weekly_log(text: str, root: Path) -> Path:
    """Añade el reporte al log estable ``logs/recall-weekly-report.log`` (append).

    Crea el archivo si no existe; fail-open si el directorio no existe o la
    escritura falla (nunca interrumpe el script).

    Args:
        text: Texto del reporte ya formateado.
        root: Raíz del repo ARIS4U (ARIS4U_ROOT).

    Returns:
        Ruta del archivo de log (``root/logs/recall-weekly-report.log``).
    """
    log_path = root / "logs" / "recall-weekly-report.log"
    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n" + text + "\n")
    except OSError:
        pass
    return log_path


# --- Orquestación CLI -----------------------------------------------------------------

def evaluate(events: list[dict], projects_dir: Path) -> tuple[list[dict], dict]:
    """Juzga cada evento instrumentado; devuelve (resultados, conteos de descarte).

    Args:
        events: Eventos auto_recall de la ventana.
        projects_dir: Raíz de transcripts.

    Returns:
        ``(results, skips)`` donde results = [{recall_id, useful, score, matched, query}]
        y skips = {no_session, no_transcript, no_response}.
    """
    results: list[dict] = []
    skips = {"no_session": 0, "no_transcript": 0, "no_response": 0}
    for ev in events:
        sid = ev.get("session_id", "")
        injected = ev.get("injected") or []
        if not sid or not injected:
            skips["no_session"] += 1
            continue
        tr = find_transcript(sid, projects_dir)
        if tr is None:
            skips["no_transcript"] += 1
            continue
        # session_start recalls: no hay prompt-disparador (el recall se inyecta antes
        # de cualquier mensaje del usuario). Evaluar contra el texto completo de la
        # sesión: si el briefing fue usado, algún término aparecerá en algún turno.
        if ev.get("source") == "session_start":
            resp: str | None = _full_session_text(tr) or None
        else:
            resp = extract_response(tr, ev.get("query", ""))
        if not resp:
            skips["no_response"] += 1
            continue
        useful, score, matched = judge(injected, ev.get("query", ""), resp)
        results.append({
            "recall_id": ev.get("recall_id", ""),
            "useful": useful,
            "score": score,
            "matched": matched,
            "query": ev.get("query", ""),
            "client": ev.get("client", ""),
            "source": ev.get("source", ""),
        })
    return results, skips


def _print_report(results: list[dict], skips: dict, days: int, applied: bool) -> None:
    """Imprime la tabla de veredictos + el resumen de la métrica."""
    mode = "APLICADO" if applied else "DRY-RUN (no escribe)"
    print(f"\n=== CALIFICADOR DE RECALLS · últimos {days} días · {mode} ===")
    judged = len(results)
    useful = sum(1 for r in results if r["useful"])
    if judged:
        print(f"\n{'recall_id':14s} {'útil':5s} {'score':5s} términos usados / query")
        for r in sorted(results, key=lambda x: (-x["score"], x["recall_id"])):
            flag = "✓" if r["useful"] else "·"
            terms = ", ".join(r["matched"][:6]) or "—"
            print(f"  {r['recall_id']:12s} {flag:^5s} {r['score']:>4.0f}  {terms[:60]}")
            print(f"                            ↳ {r['query'][:64]}")
    skipped = sum(skips.values())
    # session_start recalls are evaluated separately (not counted in the main
    # useful% metric — their judge() baseline is the whole session, not a query,
    # so mixing them would deflate the real-query utility rate).
    ss_results = [r for r in results if r.get("source") == "session_start"]
    real_results = [r for r in results if r.get("source") != "session_start"]
    real_useful = sum(1 for r in real_results if r["useful"])
    real_judged = len(real_results)
    weeks = max(1.0, days / 7)
    print(f"\nJuzgados (prompts reales): {real_judged}  "
          f"·  ÚTILES: {real_useful}  ·  no-útiles: {real_judged - real_useful}")
    if ss_results:
        ss_useful = sum(1 for r in ss_results if r["useful"])
        print(f"Session-start (separados, no cuentan en %): "
              f"{len(ss_results)} juzgados · {ss_useful} útiles")
    print(f"Sin juzgar: {skipped} "
          f"(sin instrumentar: {skips['no_session']}, sin transcript: {skips['no_transcript']}, "
          f"sin respuesta emparejable: {skips['no_response']})")
    print(f"→ útiles/semana ≈ {useful / weeks:.1f}  "
          f"(umbral éxito >=3 sostenido 2 sem; <1 = re-diagnosticar)")


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada CLI."""
    ap = argparse.ArgumentParser(description="Calificador automático de utilidad de recalls")
    ap.add_argument("--days", type=int, default=7, help="ventana en días (default 7)")
    ap.add_argument("--all", action="store_true", help="ignorar la ventana temporal")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="persistir las marcas implícitas")
    g.add_argument("--dry-run", action="store_true", help="solo mostrar (default)")
    g.add_argument(
        "--stats",
        action="store_true",
        help="mostrar recalls/semana y %% útiles desde SQL (recall_events)",
    )
    g.add_argument(
        "--report",
        action="store_true",
        help=(
            "generar reporte semanal de amplificación (esta sem vs anterior, "
            "tendencia, gate Tramo 4) y hacer append a logs/recall-weekly-report.log"
        ),
    )
    ap.add_argument(
        "--sync",
        action="store_true",
        help=(
            "con --stats/--report: importar eventos del JSONL a recall_events "
            "antes de calcular"
        ),
    )
    args = ap.parse_args(argv)

    root = _root()
    log_override = os.environ.get("ARIS4U_EVENTS_LOG")
    log_path = Path(log_override) if log_override else root / "logs" / "v16.1-events.jsonl"
    db_path = root / "data" / "sessions.db"
    since = None if args.all else datetime.now(UTC) - timedelta(days=args.days)

    # Modo --stats: métricas de recall desde SQL (recall_events JOIN recall_feedback).
    if args.stats:
        if not db_path.exists():
            print(f"sessions.db no encontrada en {db_path}", file=sys.stderr)
            return 1
        conn = sqlite3.connect(str(db_path))
        try:
            ensure_recall_events_schema(conn)
            synced = sync_jsonl_to_sql(conn, log_path, since) if args.sync else 0
            stats = stats_from_sql(conn, args.days)
        finally:
            conn.close()
        _print_stats_report(stats, args.days, synced)
        return 0

    # Modo --report: reporte semanal de amplificación (esta sem vs anterior, gate Tramo 4).
    if args.report:
        if not db_path.exists():
            print(f"sessions.db no encontrada en {db_path}", file=sys.stderr)
            return 1
        conn = sqlite3.connect(str(db_path))
        try:
            ensure_recall_events_schema(conn)
            ensure_schema(conn)
            synced = sync_jsonl_to_sql(conn, log_path, since) if args.sync else 0
            this_week, last_week = _weekly_stats(conn)
        finally:
            conn.close()
        report_text = _format_weekly_report(this_week, last_week)
        print(report_text)
        if synced:
            print(f"\n(Sincronizados {synced} eventos nuevos desde JSONL)")
        log_file = _append_to_weekly_log(report_text, root)
        print(f"\nReporte guardado en: {log_file}")
        return 0

    events = load_recall_events(log_path, since)
    results, skips = evaluate(events, _projects_dir())

    if args.apply:
        if not db_path.exists():
            print(f"sessions.db no encontrada en {db_path}", file=sys.stderr)
            return 1
        conn = sqlite3.connect(str(db_path))
        try:
            ensure_schema(conn)
            written = 0
            for r in results:
                # session_start recalls are NOT written to recall_feedback: their
                # judge() score is based on the whole session (no specific query),
                # so they almost always return useful=False and drag the % metric
                # down (e.g. 321 session_start recalls → 0.9% utility). They are
                # still evaluated and shown in the dry-run report for visibility,
                # but only real user-prompt recalls drive the recall_feedback table.
                if r.get("source") == "session_start":
                    continue
                detail = json.dumps(r["matched"], ensure_ascii=False)
                if upsert_implicit(conn, r["recall_id"], r["useful"], r["score"], detail):
                    written += 1
        finally:
            conn.close()
        _print_report(results, skips, args.days, applied=True)
        print(f"\nEscritas {written} marcas implícitas en recall_feedback "
              f"(marcas manuales respetadas).")
    else:
        _print_report(results, skips, args.days, applied=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
