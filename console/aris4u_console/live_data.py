#!/usr/bin/env python3
"""Lectores VIVOS de ARIS4U — conecta lo que el motor HACE, no solo lo que ES.

La Console deja de ser un explorador de archivos: lee las CONDUCTAS vivas de ARIS4U
directamente de sus fuentes de verdad (mayormente read-only, fail-soft — nunca rompe el
servidor). Excepción legítima: ``append_label`` escribe al event log para registrar
feedback F1 (etiquetar llamadas del amplificador como útil/no-útil).

  - Memoria      (``data/sessions.db``):     decisiones/guards/digests por cliente + medidor de
                                             recall (``recall_feedback``) + conteo de vectores.
  - Telemetría   (``logs/v16.1-events.jsonl``): el flujo de eventos — qué hooks corren, recalls,
                                             model_hint, guards que bloquean. "Ver ARIS4U pensar."
  - Hooks/guards (``hooks/hooks.json`` + ``~/.claude/settings.json``): qué eventos están cableados
                                             (repo + global) y cuáles dispararon (de la telemetría).

Las MCP tools (A3) se invocan por subprocess desde ``server.py`` (necesitan el venv del motor).

Filosofía (handover §3): el inventario ya cubre 100% de los ARCHIVOS; esto cierra el 100% de las
CONDUCTAS. Todas las cifras nacen de la fuente viva, nunca de docs/memoria (anti-drift).
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date, timedelta, UTC
from functools import lru_cache
from pathlib import Path

DEFAULT_REPO = Path.home() / "projects" / "aris4u"

_DB = "data/sessions.db"
_VEC = "data/aris_vectors.db"
_EVENTS = "logs/v16.1-events.jsonl"
_HOOKS_JSON = "hooks/hooks.json"

# Eventos del ciclo de vida de Claude Code que ARIS4U puede cablear (orden de presentación).
_LIFECYCLE = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
              "SubagentStart", "SubagentStop", "Stop", "SessionEnd", "PreCompact"]

# Mapeo handler→evento del ciclo de vida, derivado de dispatch/events/*.py y handlers/*.py.
# Clave = valor del campo "hook" en telemetría; Valor = evento Claude Code correspondiente.
# Evidencia file:line (sólo handlers que confirman escribir al log con ese "hook"):
#   post_agent_verify → Stop:        stop.py:284,353
#   subagent_depth    → SubagentStart: subagent_start.py:202
#   mcp_guard         → PreToolUse:  pre_tool_use.py:119; mcp_guard.py:84
#   migration_linter  → PreToolUse:  pre_tool_use.py:117; migration_linter.py:118,136
#   phi_guard         → PreToolUse:  pre_tool_use.py:118; phi_guard.py:142,145
#   phi_sanitizer     → PreToolUse:  pre_tool_use.py:119; phi_sanitizer.py:49
#   agent_dispatched  → PostToolUse: post_tool_use.py:34,101; agent_dispatched.py:62
#   schema_drift      → PostToolUse: post_tool_use.py:36,105; schema_drift.py:227
#   redact_secrets    → PostToolUse: post_tool_use.py:43-55 (_log_secret_redacted)
# Handlers SIN evento confirmado (bucket "otros"): mcp_server (integrations/mcp_server.py,
# no es un hook CC), f1_feedback (amplificador F1, no es un hook CC).
_HANDLER_TO_EVENT: dict[str, str] = {
    "post_agent_verify": "Stop",
    "subagent_depth":    "SubagentStart",
    "mcp_guard":         "PreToolUse",
    "migration_linter":  "PreToolUse",
    "phi_guard":         "PreToolUse",
    "phi_sanitizer":     "PreToolUse",
    "agent_dispatched":  "PostToolUse",
    "schema_drift":      "PostToolUse",
    "redact_secrets":    "PostToolUse",
}

# Mapeo evento→lifecycle para eventos que usan el campo "event" (sin "hook") pero corresponden
# a un ciclo de vida de Claude Code. Evidencia: telemetría 2026-06-29 muestra estos event= con
# hook=None. FIX #2 (bugs clase-A round 2): read_hooks debe mapear también este campo.
#   auto_recall             → UserPromptSubmit  (corre al recibir cada prompt)
#   capability_hint         → UserPromptSubmit  (inyecta capacidad adecuada al prompt)
#   session_briefing        → SessionStart      (ceba contexto al inicio de sesión)
#   session_end_dirty_check → SessionEnd        (verifica cambios al cerrar la sesión)
_EVENT_TO_LIFECYCLE: dict[str, str] = {
    "auto_recall":              "UserPromptSubmit",
    "capability_hint":          "UserPromptSubmit",
    "session_briefing":         "SessionStart",
    "session_end_dirty_check":  "SessionEnd",
}


# --- utilidades read-only -------------------------------------------------------------

def _connect_ro(path: Path) -> sqlite3.Connection | None:
    """Abre una SQLite en modo SOLO-LECTURA (URI mode=ro). None si no existe o falla."""
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """Primer valor de una query (0 si la tabla no existe o falla)."""
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0]) if row and row[0] is not None else 0
    except sqlite3.Error:
        return 0


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Filas como dicts (lista vacía si falla)."""
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def tail_lines(path: Path, n: int) -> list[str]:
    """Últimas ``n`` líneas no vacías de un archivo de texto, leyendo bloques desde el final.

    Eficiente en archivos que crecen (no carga el archivo entero). Tolera UTF-8 partido en el
    borde del bloque usando ``errors='replace'``.
    """
    if n <= 0 or not path.is_file():
        return []
    try:
        size = path.stat().st_size
        block = 65536
        data = b""
        with path.open("rb") as f:
            pos = size
            while pos > 0 and data.count(b"\n") <= n:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                data = f.read(step) + data
        text = data.decode("utf-8", errors="replace")
        return [ln for ln in text.splitlines() if ln.strip()][-n:]
    except OSError:
        return []


def parse_events(lines: list[str]) -> list[dict]:
    """Parsea líneas JSONL a dicts, descartando las corruptas (fail-soft)."""
    out: list[dict] = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except (json.JSONDecodeError, ValueError):
            continue
    return out


# --- A1: Memoria ----------------------------------------------------------------------

# Filtro de "decisión REAL" para la BITÁCORA: excluye provenance (git-commits) Y facts (átomos
# de método). Los átomos viven en su propia sección 🧬 Átomos; Memoria = bitácora de decisiones.
_REAL = "(mem_type IS NULL OR mem_type NOT IN ('provenance','fact'))"


def _by_client(conn: sqlite3.Connection, table: str, where: str = "") -> dict[str, int]:
    """Conteo por client_id. NULL **y cadena vacía** → '(none)' (un solo bucket sin-proyecto).
    ``where`` opcional (p.ej. excluir provenance en decisions)."""
    out: dict[str, int] = {}
    clause = f" WHERE {where}" if where else ""
    for r in _rows(conn, f"SELECT COALESCE(NULLIF(client_id,''),'(none)') c, count(*) n "
                         f"FROM {table}{clause} GROUP BY c"):
        out[r["c"]] = out.get(r["c"], 0) + r["n"]
    return out


def _vectors_count(repo: Path) -> int:
    """Cuenta los vectores del sidecar (data/aris_vectors.db); 0 si no existe."""
    conn = _connect_ro(repo / _VEC)
    if conn is None:
        return 0
    try:
        # vec_map = lado de metadata (1 fila por vector); fallback a vec_items.
        n = _scalar(conn, "SELECT count(*) FROM vec_map")
        return n or _scalar(conn, "SELECT count(*) FROM vec_items")
    finally:
        conn.close()


def recall_stats(conn: sqlite3.Connection, events: list[dict]) -> dict:
    """Medidor de recall: feedback explícito/implícito + actividad de recall en la telemetría.

    Args:
        conn: Conexión read-only a sessions.db.
        events: Eventos recientes (de la ventana de telemetría) para contar auto_recall.

    Returns:
        Dict con total de feedback, % útil, recalls recientes y el último recall observado.
    """
    total = _scalar(conn, "SELECT count(*) FROM recall_feedback")
    useful = _scalar(conn, "SELECT count(*) FROM recall_feedback WHERE useful=1")
    recalls = [e for e in events if e.get("event") == "auto_recall"]
    last = recalls[-1] if recalls else None
    return {
        "feedback_total": total,
        "feedback_useful": useful,
        "useful_rate": round(useful / total, 2) if total else None,
        "recalls_in_window": len(recalls),
        # `or ""` (no el default de .get) por si el campo está presente pero es None → no crashea
        "last_recall_query": ((last or {}).get("query") or "")[:80],
        "last_recall_ts": (last or {}).get("ts") or "",
    }


# Proyectos: nombre para mostrar (MAYÚSCULAS) + explicación de qué es cada uno.
# (lab-project-1-legacy = lab-project-1 (old name) · client-d · (none) at end).
_PROJECTS = {
    "aris4u": ("ARIS4U", "El amplificador mismo: este sistema que potencia a el usuario sobre Claude."),
    "client-c": ("CLIENT-C", "Client C: radiology client. Inventory system + CRM."),
    "client-b": ("CLIENT-B", "Client B: all-in-one platform (modules, AI, omnichannel) — revenue client."),
    "client-e": ("CLIENT-E", "Client E: real estate brokerage."),
    "lab-project-4": ("LAB-PROJECT-4", "Lab Project 4: RTCC/COP platform concept. "
                   "Own project in plan phase; scene3d paused (GPU crash)."),
    "lab-project-1": ("LAB-1 · serie", "Lab-1 series content: novel + manga + animation. img2video production. "
               "Separate from the app — design/animation content only."),
    "lab-project-1-app": ("LAB-1 · app", "Lab-1 Flutter ride/dating app (points, rides, surge, matching). "
                  "Separate from series — software only."),
    "pentest": ("PENTEST", "Conocimiento de seguridad ofensiva (vertical diferido, gated por el CVP)."),
    "quimera": ("QUIMERA", "Contrato del RPG por turnos estilo Final Fantasy (proyecto de juego)."),
    "lab-project-3": ("REPRESENTATION ENGINE",
                              "Exploración fundacional: representación / compresión / predicción."),
    "client-d": ("CLIENT-D",
                "Client D (user = CTO): SaaS products."),
    "(none)": ("GLOBAL / transferible",
               "Conocimiento que NO es de un solo proyecto, por diseño: átomos de método "
               "(patrones reutilizables) + decisiones globales. Transferible entre proyectos."),
}


def project_label(cid: str) -> str:
    """Nombre para mostrar de un proyecto (MAYÚSCULAS); fallback = el id en mayúsculas."""
    return _PROJECTS[cid][0] if cid in _PROJECTS else (cid or "(none)").upper()


def project_about(cid: str) -> str:
    """Explicación de qué es un proyecto (para el clic en la tabla)."""
    return _PROJECTS.get(cid, ("", "Proyecto sin descripción registrada."))[1]


def _atom_name(skeleton: str, decision: str, problem_class: str) -> str:
    """Nombre legible de un átomo: 'name:' explícito → clase/fn (saltando comentarios) →
    el problem_class legible. Evita capturar líneas de comentario (--, //, #)."""
    import re
    text = skeleton or decision or ""
    m = re.search(r"\[atom:([A-Za-z0-9_\-]{3,})\]", text)  # átomos conceptuales [atom:X]
    if m:
        return m.group(1)
    m = re.search(r"\bname\s*[:=]\s*([A-Za-z0-9_\-]{3,})", text)
    if m:
        return m.group(1)
    body = "\n".join(ln for ln in text.splitlines()
                     if not ln.strip().startswith(("--", "//", "#")))
    m = re.search(r"(?:clase|class|fn|table|función|funcion)\s*[:=]\s*([A-Za-z0-9_\-]{3,})", body)
    if m:
        return m.group(1)
    return (problem_class or "átomo").replace("-", " ")


def _atom_value(validity: str, adoption: str) -> str:
    """Valor del átomo: descartado/bajo (flag en validity) o valioso/catálogo (por adoption)."""
    if validity.startswith("[DESCARTADO"):
        return "descartado"
    if validity.startswith("[BAJO"):
        return "bajo"
    return "valioso" if adoption == "used" else "catálogo"


def _atom_row(d: dict) -> dict:
    """Enriqucece una fila de átomo: nombre legible, proyecto de origen, valor."""
    # el nombre 'name:X' vive en el contenido (decision); skeleton es el esqueleto técnico
    d["name"] = _atom_name(d["decision"], d["skeleton"], d["problem_class"] or d["artifact_type"])
    origin = d["source_project"] or d["scope"]
    d["project"] = project_label(origin) if origin else "GLOBAL"
    d["skeleton"] = d["skeleton"] or d["decision"]
    d["value"] = _atom_value(d["validity_domain"], d["adoption"])
    # transfers_to can be JSON ["client-a","client-c"...] → etiquetas de proyecto; o '' / texto viejo
    try:
        codes = json.loads(d["transfers_to"]) if d["transfers_to"] else []
        d["transfers"] = [project_label(x) for x in codes] if isinstance(codes, list) else []
    except (ValueError, TypeError):
        d["transfers"] = []
    d.pop("decision", None)
    return d


def read_atoms(repo: Path | None = None) -> dict:
    """Lee los átomos de método (mem_type='fact' con problem_class) con TODOS sus campos:
    qué es (name/class/artifact), cómo se compone (skeleton), para qué (validity_domain),
    y en qué proyecto se usa (source_project / scope / transfers / adoption). Read-only."""
    repo = repo or DEFAULT_REPO
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return {"available": False, "reason": f"no se encontró {_DB}"}
    try:
        rows = _rows(conn,
            "SELECT rowid AS id, decision, COALESCE(domain,'') domain, "
            "COALESCE(problem_class,'') problem_class, COALESCE(artifact_type,'') artifact_type, "
            "COALESCE(regime,'') regime, COALESCE(skeleton,'') skeleton, "
            "COALESCE(validity_domain,'') validity_domain, COALESCE(transfers_to,'') transfers_to, "
            "COALESCE(adoption,'') adoption, COALESCE(evidence_kind,'') evidence_kind, "
            "COALESCE(source_project,'') source_project, "
            "COALESCE(NULLIF(client_id,''),'') scope FROM decisions "
            # Solo átomos de método REALES (con firma estructural). Los knowledge-facts
            # conceptuales pre-sistema (teoremas lab-project-3, etc.) no son átomos
            # de método: viven en Memoria, no contaminan /atoms. Coherente con read_valorizacion.
            "WHERE mem_type='fact' AND structural_signature IS NOT NULL AND structural_signature != '' "
            "ORDER BY (source_project IS NULL OR source_project=''), "
            "COALESCE(source_project,''), COALESCE(client_id,''), "
            "COALESCE(problem_class, artifact_type, '')")
        atoms = [_atom_row(dict(r)) for r in rows]
        return {"available": True, "atoms": atoms, "total": len(atoms)}
    finally:
        conn.close()


# --- A5: Valorización RICE-A+Moat -------------------------------------------------------

# Fórmula elegida y justificación (documenta la decisión en el código):
#
# RICE-A score = (Reach × Impact × Confidence × Adoption) / Effort
#
# Reach:       Número de proyectos DISTINTOS en los que el átomo está presente o
#              se transfiere (source_project + elementos válidos del JSON en transfers_to).
#              Rango natural 0–N; cap en 5 para no sobre-ponderar átomos muy globales
#              vs. átomos muy bien calibrados en un dominio estrecho.
#
# Impact:      Fuerza de la evidencia: calibrated=3 (probado y medido), catalog=2
#              (documentado pero sin uso), ninguno=1 (solo registrado).
#
# Confidence:  Confianza en el impacto: adopted+calibrated → 0.9 (alta), calibrated
#              sin uso → 0.7 (media-alta), sin calibrar → 0.5 (baja).
#
# Effort:      Coste de adopción: 1 si tiene validity_domain documentado (fácil de
#              aplicar), 2 si no está documentado (requiere inferencia del caller).
#
# Adoption(A): Factor de uso real: used=1.0, unused=0.7, null=0.5.
#
# Moat:        Número de proyectos en transfers_to (solo entradas JSON válidas).
#              Mide la "defensibilidad / transferibilidad" del átomo: cuanto más
#              proyectos puede servir, más estratégico es mantenerlo bien documentado.
#              Rango 0–5 (cap en 5).
#
# Veredicto:
#   adopt → score ≥ 3.0 Y moat ≥ 2  (listo para usar, ya probado y transferible)
#   build → score ≥ 1.5 O moat ≥ 1  (promisorio, vale documentar mejor o extender)
#   omit  → resto                    (bajo valor, descartado o faltan metadatos clave)

_EV_IMPACT = {"calibrated": 3, "catalog": 2}
_AD_CONFIDENCE = {"used": 0.9, "unused": 0.7}
_AD_FACTOR = {"used": 1.0, "unused": 0.7}


def _rice_reach(source_project: str, transfers_to: str) -> int:
    """Reach = proyectos distintos donde el átomo está presente o se transfiere.

    Args:
        source_project: Proyecto de origen del átomo (puede ser '').
        transfers_to: Campo raw de la DB (JSON list de project codes, o texto libre).

    Returns:
        Número de proyectos distintos, capped a 5.
    """
    projs: set[str] = set()
    if source_project:
        projs.add(source_project)
    if transfers_to:
        try:
            codes = json.loads(transfers_to)
            if isinstance(codes, list):
                for c in codes:
                    if isinstance(c, str) and c.strip():
                        projs.add(c.strip())
        except (ValueError, TypeError):
            pass
    return min(len(projs), 5)


def _rice_score(reach: int, evidence_kind: str, adoption: str,
                has_validity: bool) -> float:
    """RICE-A score = (R × I × C × A) / E.

    Args:
        reach: Número de proyectos (0–5).
        evidence_kind: 'calibrated' | 'catalog' | ''.
        adoption: 'used' | 'unused' | ''.
        has_validity: True si validity_domain está documentado.

    Returns:
        Score RICE-A redondeado a 2 decimales.
    """
    impact = _EV_IMPACT.get(evidence_kind, 1)
    confidence = _AD_CONFIDENCE.get(adoption, 0.5) if evidence_kind else 0.5
    adop_factor = _AD_FACTOR.get(adoption, 0.5)
    effort = 1 if has_validity else 2
    return round((reach * impact * confidence * adop_factor) / effort, 2)


def _moat(transfers_to: str) -> int:
    """Moat = longitud del JSON array en transfers_to (0 si no es JSON válido).

    El moat mide cuántos proyectos pueden beneficiarse del átomo — su
    defensibilidad/transferibilidad. Solo cuentan entradas JSON válidas (no texto libre).

    Args:
        transfers_to: Campo raw de la DB.

    Returns:
        Conteo capped a 5.
    """
    if not transfers_to:
        return 0
    try:
        codes = json.loads(transfers_to)
        if isinstance(codes, list):
            return min(len([c for c in codes if isinstance(c, str) and c.strip()]), 5)
    except (ValueError, TypeError):
        pass
    return 0


def _verdict(score: float, moat: int) -> str:
    """Veredicto RICE-A+Moat: adopt / build / omit.

    Args:
        score: RICE-A score calculado.
        moat: Moat calculado.

    Returns:
        'adopt' | 'build' | 'omit'.
    """
    if score >= 3.0 and moat >= 2:
        return "adopt"
    if score >= 1.5 or moat >= 1:
        return "build"
    return "omit"


def _rice_row(a: dict) -> dict:
    """Añade campos RICE-A+Moat a un dict de átomo (enriquecido por _build_rice_atom).

    Args:
        a: Átomo enriquecido (con raw_transfers, adoption, evidence_kind, validity_domain, etc;
           preparado por ``_build_rice_atom``, no por ``_atom_row`` que sigue otro camino).

    Returns:
        El mismo dict con campos rice_reach, rice_score, moat, verdict añadidos.
    """
    reach = _rice_reach(
        a.get("source_project", ""),
        a.get("raw_transfers", ""),
    )
    evidence_kind = a.get("evidence_kind", "")
    adoption = a.get("adoption", "")
    has_validity = bool((a.get("validity_domain") or "").strip())
    score = _rice_score(reach, evidence_kind, adoption, has_validity)
    moat_val = _moat(a.get("raw_transfers", ""))
    a["rice_reach"] = reach
    a["rice_score"] = score
    a["moat"] = moat_val
    a["verdict"] = _verdict(score, moat_val)
    return a


def _parse_transfers_labels(transfers_to: str) -> list[str]:
    """Parsea transfers_to JSON → lista de etiquetas de proyecto legibles.

    Args:
        transfers_to: Campo raw de la DB (JSON array de project codes, o texto libre).

    Returns:
        Lista de nombres legibles; lista vacía si no es JSON válido o está vacío.
    """
    if not transfers_to:
        return []
    try:
        codes = json.loads(transfers_to)
        if isinstance(codes, list):
            return [project_label(x) for x in codes if isinstance(x, str) and x.strip()]
    except (ValueError, TypeError):
        pass
    return []


def _build_rice_atom(row: dict) -> dict:
    """Convierte una fila raw de la DB en un átomo valorado (RICE-A + Moat + veredicto).

    Extrae la lógica de enriquecimiento del loop de read_valorizacion para mantener CC baja.

    Args:
        row: Dict con los campos de la query (decision, skeleton, problem_class, etc.).

    Returns:
        Dict enriquecido listo para la respuesta (sin el campo 'decision').
    """
    d = dict(row)
    d["name"] = _atom_name(d["decision"], d["skeleton"],
                           d["problem_class"] or d["artifact_type"])
    d["project"] = project_label(d["source_project"]) if d["source_project"] else "GLOBAL"
    d["raw_transfers"] = d["transfers_to"]
    d["transfers"] = _parse_transfers_labels(d["transfers_to"])
    _rice_row(d)
    d.pop("decision", None)
    return d


def _verdict_totals(atoms: list[dict]) -> dict[str, int]:
    """Cuenta átomos por veredicto para el resumen de cabecera.

    Args:
        atoms: Lista de átomos ya valorados (con campo 'verdict').

    Returns:
        Dict {'adopt': N, 'build': N, 'omit': N}.
    """
    totals: dict[str, int] = {"adopt": 0, "build": 0, "omit": 0}
    for a in atoms:
        v = a.get("verdict", "omit")
        totals[v] = totals.get(v, 0) + 1
    return totals


_RICE_SQL = (
    "SELECT rowid AS id, decision, "
    "COALESCE(problem_class,'') problem_class, COALESCE(artifact_type,'') artifact_type, "
    "COALESCE(regime,'') regime, COALESCE(skeleton,'') skeleton, "
    "COALESCE(validity_domain,'') validity_domain, "
    "COALESCE(transfers_to,'') transfers_to, "
    "COALESCE(adoption,'') adoption, COALESCE(evidence_kind,'') evidence_kind, "
    "COALESCE(source_project,'') source_project, "
    "COALESCE(NULLIF(client_id,''),'') scope FROM decisions "
    "WHERE mem_type='fact' AND structural_signature IS NOT NULL "
    "AND structural_signature != '' "
    "ORDER BY COALESCE(source_project,''), COALESCE(problem_class,'')"
)


def read_valorizacion(repo: Path | None = None) -> dict:
    """Valora los átomos de método con framework RICE-A+Moat → veredicto adopt/build/omit.

    Lee read-only desde data/sessions.db (solo mem_type='fact' con structural_signature).
    Cada átomo recibe: Reach, Impact, Confidence, Effort, Adoption (RICE-A) y Moat
    (transferibilidad). El veredicto sintetiza si el átomo está listo para usar,
    merece inversión o tiene bajo valor ahora.

    Fórmula: score = (R × I × C × A) / E. Ver comentario de módulo en _rice_score.

    Args:
        repo: Raíz del repo ARIS4U (default DEFAULT_REPO).

    Returns:
        Dict con ``available``, ``atoms`` (ordenados por score desc), ``totals``
        (conteos por veredicto), ``total``. ``available=False`` si no hay DB.
    """
    repo = repo or DEFAULT_REPO
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return {"available": False, "reason": f"no se encontró {_DB}"}
    try:
        atoms = [_build_rice_atom(r) for r in _rows(conn, _RICE_SQL)]
        atoms.sort(key=lambda x: (-x["rice_score"], -x["moat"]))
        return {
            "available": True,
            "atoms": atoms,
            "totals": _verdict_totals(atoms),
            "total": len(atoms),
        }
    finally:
        conn.close()


# --- A5b: Backlog de adopción (lo accionable de Valorización) ---------------------------

# Mapa código-de-proyecto → repo en disco. Verifica el FIT del backlog: el transfers_to lo
# infirió la minería SIN comprobar que el destino exista o encaje. Sin esto, el backlog
# recomienda adoptar patrones en proyectos ausentes o que ya los tienen.
_PROJECTS_HOME = Path.home() / "projects"
_PROJECT_REPOS: dict[str, str] = {
    "client-b": "client-b-platform",
    "client-a": "client-a",
    "client-c": "client-c",
    "client-e": "client-e",
    "client-d": "client-d",
    "lab-project-1-app": "lab-project-1-app",
    "lab-project-4": "lab-project-4",
}
# artifact_types que REQUIEREN una base de datos SQL/Postgres: un destino sin migraciones
# no puede "adoptar" estos patrones (mismatch arquitectónico — p.ej. un sitio Astro no puede
# adoptar un ledger-append-only ni un schema-migration).
_DB_REQUIRED = {"multi-tenant-isolation", "access-control", "schema-migration",
                "ledger-append-only"}
# subconjunto RLS puro: en un destino RLS-maduro el patrón probablemente YA existe (redundante).
_RLS_FAMILY = {"multi-tenant-isolation", "access-control"}
_MATURE_MIGRATIONS = 20  # ≥ esto = destino RLS-maduro → el patrón RLS probablemente ya existe


@lru_cache(maxsize=64)
def _project_profile(code: str) -> tuple[bool, bool, int]:
    """Perfil en disco de un proyecto destino (cacheado por proceso). Read-only y barato.

    Returns:
        ``(present, is_rls, migrations)``: si hay código fuente real, si tiene migraciones
        SQL (arquitectura RLS/Postgres), y cuántas (señal de madurez). Todo False/0 si el
        proyecto no está mapeado o su carpeta no existe.
    """
    rel = _PROJECT_REPOS.get(code)
    if not rel:
        return (False, False, 0)
    base = _PROJECTS_HOME / rel
    if not base.is_dir():
        return (False, False, 0)
    # Presente = repo real con código (no solo un stub vacío). El código
    # vive en subdirs de profundidad variable, así que se busca un archivo fuente o marcador
    # de proyecto a profundidad 0–2 con glob LAZY (next() corta en el primer match → barato).
    _present_globs = (
        ".git", "package.json", "pyproject.toml", "go.mod", "pom.xml", "Cargo.toml",
        "*/package.json", "*/pyproject.toml", "*/*/package.json", "*/*/pyproject.toml",
        "*.py", "*/*.py", "*/*/*.py", "*.sql", "*/*.sql", "*/*/*.sql",
    )
    present = any(next(base.glob(g), None) is not None for g in _present_globs)
    # Migraciones SQL (señal de arquitectura RLS/Postgres) a profundidad 0–2 (glob acotado,
    # nunca ** recursivo sobre repos de varios GB).
    migrations = sum(
        1
        for pat in ("supabase/migrations/*.sql", "*/supabase/migrations/*.sql",
                    "*/*/supabase/migrations/*.sql", "migrations/*.sql", "*/migrations/*.sql")
        for _ in base.glob(pat)
    )
    return (present, migrations > 0, migrations)


def _fit_status(artifact_type: str, target_code: str) -> str:
    """Adopción de un patrón en un destino: absent | mismatch | likely-present | candidate.

    - absent: el código del destino no está en disco (no construible aquí).
    - mismatch: patrón RLS/Postgres pero el destino no tiene esa arquitectura.
    - likely-present: destino RLS-maduro → el patrón RLS probablemente ya existe (redundante).
    - candidate: presente, encaja y no es obviamente redundante → vale analizar a fondo.
    """
    present, is_rls, migrations = _project_profile(target_code)
    if not present:
        return "absent"
    if artifact_type in _DB_REQUIRED and not is_rls:
        return "mismatch"  # patrón de DB en un destino sin migraciones SQL
    if artifact_type in _RLS_FAMILY and migrations >= _MATURE_MIGRATIONS:
        return "likely-present"  # destino RLS-maduro → probablemente ya lo tiene
    return "candidate"


def _project_atom_onto_targets(a: dict, by_code: dict[str, dict]) -> None:
    """Emite un ítem por cada proyecto destino del átomo (excluyendo origen), con su fit verificado.

    Itera los CÓDIGOS crudos de transfers_to (no las etiquetas) para poder mapear cada destino
    a su repo en disco y calcular el fit. Normalizes origin (client-c-inventory → client-c) to avoid
    proponer que un átomo se "adopte" en su propio proyecto.

    Args:
        a: Átomo valorado (name, source_project, raw_transfers, rice_score, moat, verdict, ...).
        by_code: Acumulador {target_code: {label, items[]}} (mutado in-place).
    """
    origin = (a.get("source_project") or "").strip().lower()
    origin_norm = origin.replace("-inventory", "")
    at = a.get("artifact_type", "")
    try:
        codes = json.loads(a.get("raw_transfers", "") or "[]")
    except (ValueError, TypeError):
        codes = []
    for code in codes:
        if not isinstance(code, str):
            continue
        code = code.strip().lower()
        if not code or code in (origin, origin_norm):
            continue  # un átomo no se "adopta" en su propio origen
        bucket = by_code.setdefault(code, {"label": project_label(code), "items": []})
        bucket["items"].append({
            "pattern": a.get("name", ""),
            "origin": a.get("project", ""),
            "score": a.get("rice_score", 0),
            "moat": a.get("moat", 0),
            "verdict": a.get("verdict", ""),
            "problem_class": a.get("problem_class", ""),
            "artifact_type": at,
            "fit": _fit_status(at, code),
            "why": (a.get("validity_domain") or "").strip()[:180],
        })


def _classify_backlog(by_code: dict[str, dict],
                      fit: tuple[str, ...]) -> tuple[list[dict], dict[str, dict], Counter]:
    """Clasifica los buckets por destino en grupos mostrados vs filtrados, y cuenta el fit.

    Args:
        by_code: {target_code: {label, items[]}} con cada item ya marcado con su 'fit'.
        fit: estados de fit a MOSTRAR (el resto se filtra a filtered_projects).

    Returns:
        ``(groups, filtered_projects, fit_totals)`` — grupos mostrados (con items ordenados por
        score), destinos descartados enteros {label: {reason, count}}, y el conteo global por fit.
    """
    fit_totals: Counter = Counter()
    groups: list[dict] = []
    filtered_projects: dict[str, dict] = {}
    for code, bucket in by_code.items():
        items = bucket["items"]
        for it in items:
            fit_totals[it["fit"]] += 1
        shown = [it for it in items if it["fit"] in fit]
        if shown:
            shown.sort(key=lambda x: -x["score"])
            groups.append({"project": bucket["label"], "code": code,
                           "count": len(shown), "items": shown})
        else:
            reason = Counter(it["fit"] for it in items).most_common(1)[0][0]
            filtered_projects[bucket["label"]] = {"reason": reason, "count": len(items)}
    return groups, filtered_projects, fit_totals


def read_backlog(repo: Path | None = None,
                 verdicts: tuple[str, ...] = ("adopt", "build"),
                 fit: tuple[str, ...] = ("candidate",)) -> dict:
    """Backlog de adopción VERIFICADO: qué patrón probado vale construir en cada destino.

    Operacionaliza Valorización con un gate de realidad: proyecta cada átomo adopt/build
    sobre sus proyectos destino, pero clasifica el FIT de cada par (patrón, destino) —
    absent (sin código en disco) · mismatch (arquitectura incompatible) · likely-present
    (destino RLS-maduro, ya lo tiene) · candidate (presente, encaja, no redundante). Por
    defecto solo surface los ``candidate`` — sin esto el backlog recomendaría los ~265 ítems
    phantom items surfaced by the verification (absent projects, non-RLS projects, mature projects).

    Reusa el MISMO scorer que read_valorizacion. Vivo (DOCS-FRESH). El perfil de cada repo se
    cachea por proceso (_project_profile).

    Args:
        repo: Raíz del repo ARIS4U (default DEFAULT_REPO).
        verdicts: Veredictos a incluir (default adopt+build; omit nunca entra).
        fit: Estados de fit a mostrar (default solo 'candidate').

    Returns:
        Dict con ``available``, ``by_project`` (grupos {project, code, count, items[]}),
        ``total_items``, ``total_projects``, ``fit_totals`` (conteo por fit sobre TODO el
        crudo), ``filtered_projects`` (destinos descartados enteros + razón dominante),
        ``fit_shown``.
    """
    repo = repo or DEFAULT_REPO
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return {"available": False, "reason": f"no se encontró {_DB}"}
    try:
        atoms = [_build_rice_atom(r) for r in _rows(conn, _RICE_SQL)]
    finally:
        conn.close()

    by_code: dict[str, dict] = {}
    for a in atoms:
        if a.get("verdict") in verdicts:
            _project_atom_onto_targets(a, by_code)

    groups, filtered_projects, fit_totals = _classify_backlog(by_code, fit)
    groups.sort(key=lambda g: -g["count"])
    return {
        "available": True,
        "by_project": groups,
        "total_items": sum(g["count"] for g in groups),
        "total_projects": len(groups),
        "fit_totals": dict(fit_totals),
        "filtered_projects": filtered_projects,
        "fit_shown": list(fit),
    }


# --- A5c: Catálogo de plantillas (skeletons reutilizables) ------------------------------

_SKEL_SQL = (
    "SELECT rowid AS id, decision, COALESCE(skeleton,'') skeleton, "
    "COALESCE(problem_class,'') problem_class, COALESCE(artifact_type,'') artifact_type, "
    "COALESCE(regime,'') regime, COALESCE(source_project,'') source_project, "
    "COALESCE(validity_domain,'') validity_domain "
    "FROM decisions WHERE mem_type='fact' AND skeleton IS NOT NULL AND skeleton != '' "
    "ORDER BY COALESCE(artifact_type,''), COALESCE(problem_class,'')"
)


def read_skeletons(repo: Path | None = None) -> dict:
    """Catálogo de PLANTILLAS reutilizables: cada átomo con su skeleton (plantilla de código).

    Es exactamente lo que el build flow inyecta cuando construyes en un dominio que matchea un
    patrón probado. Agrupado por familia (artifact_type) para navegar. Read-only, vivo.

    Returns:
        Dict con ``available``, ``by_family`` (grupos {family, count, items[{name, origin,
        problem_class, regime, lines, skeleton}]} ordenados por nº desc), ``total``.
    """
    repo = repo or DEFAULT_REPO
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return {"available": False, "reason": f"no se encontró {_DB}"}
    try:
        rows = _rows(conn, _SKEL_SQL)
    finally:
        conn.close()

    by_family: dict[str, list[dict]] = {}
    for r in rows:
        skel = r["skeleton"]
        fam = r["artifact_type"] or (r["problem_class"] and "(solo problem_class)") or "(sin clasificar)"
        by_family.setdefault(fam, []).append({
            # FIX #4: mismo orden que _atom_row y _build_rice_atom: decision primero.
            # Antes pasaba (skel, decision, …) → _atom_name usaba el skeleton como texto
            # principal → los patrones name:/atom: vivían en decision y no se encontraban
            # → 147/218 átomos mostraban nombres genéricos ('stochastic process', etc.).
            "name": _atom_name(r["decision"], skel, r["problem_class"] or r["artifact_type"]),
            "origin": project_label(r["source_project"]) if r["source_project"] else "GLOBAL",
            "problem_class": r["problem_class"],
            "regime": r["regime"],
            "lines": skel.count("\n") + 1,
            "skeleton": skel,
        })
    groups = [{"family": fam, "count": len(items), "items": items}
              for fam, items in by_family.items()]
    groups.sort(key=lambda g: -g["count"])
    return {
        "available": True,
        "by_family": groups,
        "total": len(rows),
        "families": len(groups),
    }


# --- A6: Auditoría del store de átomos --------------------------------------------------

def _audit_duplicados(conn: sqlite3.Connection) -> list[dict]:
    """Hallazgos de tipo 'duplicado': structural_signature que aparece en >1 fila.

    Args:
        conn: Conexión read-only a sessions.db.

    Returns:
        Lista de findings (uno por signature duplicada) ordenados por conteo desc.
    """
    # Dedup LÓGICO: una fila marcada con canonical_id != id es un duplicado YA resuelto
    # (apunta a su canónico). Contamos solo las canónicas/no-deduplicadas, así un grupo ya
    # deduplicado deja de reportarse. Ver dedup vía canonical_id (2026-06-29).
    dup_rows = _rows(conn, """
        SELECT structural_signature, count(*) n,
               group_concat(rowid, ',') ids
        FROM decisions
        WHERE mem_type='fact'
          AND structural_signature IS NOT NULL
          AND structural_signature != ''
          AND (canonical_id IS NULL OR canonical_id = id)
        GROUP BY structural_signature
        HAVING count(*) > 1
        ORDER BY n DESC
    """)
    return [
        {
            "type": "duplicado",
            "severity": "warn",
            "signature": r["structural_signature"],
            "count": r["n"],
            "ids": [int(x) for x in (r["ids"] or "").split(",") if x],
            "description": (
                f"structural_signature '{r['structural_signature'][:60]}' "
                f"aparece en {r['n']} filas distintas — posible deduplicar."
            ),
        }
        for r in dup_rows
    ]


def _audit_sin_validity(conn: sqlite3.Connection) -> list[dict]:
    """Hallazgo 'sin_validity': átomos sin validity_domain (no se sabe dónde aplica/rompe).

    Args:
        conn: Conexión read-only a sessions.db.

    Returns:
        Lista de 0 o 1 finding (agrupado) con los ids afectados (cap 50).
    """
    n = _scalar(conn, "SELECT count(*) FROM decisions WHERE mem_type='fact' "
                "AND (validity_domain IS NULL OR validity_domain='')")
    if n == 0:
        return []
    ids = [r["id"] for r in _rows(conn,
           "SELECT rowid id FROM decisions WHERE mem_type='fact' "
           "AND (validity_domain IS NULL OR validity_domain='') LIMIT 50")]
    return [{
        "type": "sin_validity",
        "severity": "warn",
        "count": n,
        "ids": ids,
        "description": (
            f"{n} átomos sin validity_domain — no se sabe dónde "
            "aplica ni dónde rompe. Dificulta la adopción."
        ),
    }]


def _audit_sin_source(conn: sqlite3.Connection) -> list[dict]:
    """Hallazgo 'sin_source': átomos sin source_project (origen desconocido).

    Args:
        conn: Conexión read-only a sessions.db.

    Returns:
        Lista de 0 o 1 finding (agrupado) con los ids afectados (cap 50).
    """
    n = _scalar(conn, "SELECT count(*) FROM decisions WHERE mem_type='fact' "
                "AND (source_project IS NULL OR source_project='')")
    if n == 0:
        return []
    ids = [r["id"] for r in _rows(conn,
           "SELECT rowid id FROM decisions WHERE mem_type='fact' "
           "AND (source_project IS NULL OR source_project='') LIMIT 50")]
    return [{
        "type": "sin_source",
        "severity": "info",
        "count": n,
        "ids": ids,
        "description": (
            f"{n} átomos sin source_project — origen desconocido. "
            "El RICE Reach no puede contar el proyecto de origen."
        ),
    }]


def _audit_bajo_valor(conn: sqlite3.Connection) -> list[dict]:
    """Hallazgo 'bajo_valor': validity_domain con tag [BAJO VALOR] o [DESCARTADO].

    Args:
        conn: Conexión read-only a sessions.db.

    Returns:
        Lista de 0 o 1 finding (agrupado) con hasta 20 items individuales.
    """
    bv_rows = _rows(conn, """
        SELECT rowid id, problem_class, artifact_type, validity_domain
        FROM decisions
        WHERE mem_type='fact'
          AND (validity_domain LIKE '[BAJO%' OR validity_domain LIKE '[DESCARTADO%')
        ORDER BY validity_domain
    """)
    if not bv_rows:
        return []
    return [{
        "type": "bajo_valor",
        "severity": "info",
        "count": len(bv_rows),
        "ids": [r["id"] for r in bv_rows],
        "description": (
            f"{len(bv_rows)} átomos marcados [BAJO VALOR] o [DESCARTADO] "
            "en validity_domain — candidatos a purgar del catálogo."
        ),
        "items": [
            {
                "id": r["id"],
                "label": (r["problem_class"] or r["artifact_type"] or f"#{r['id']}"),
                "tag": (r["validity_domain"] or "")[:40],
            }
            for r in bv_rows[:20]
        ],
    }]


def _audit_huecos(conn: sqlite3.Connection) -> list[dict]:
    """Hallazgo 'hueco': problem_class catalogado pero sin ningún átomo 'used'.

    Args:
        conn: Conexión read-only a sessions.db.

    Returns:
        Lista de 0 o 1 finding (agrupado) con la lista de clases huecas.
    """
    class_rows = _rows(conn, """
        SELECT problem_class,
               count(*) total,
               sum(CASE WHEN adoption='used' THEN 1 ELSE 0 END) used_count
        FROM decisions
        WHERE mem_type='fact' AND problem_class IS NOT NULL AND problem_class != ''
        GROUP BY problem_class
        HAVING sum(CASE WHEN adoption='used' THEN 1 ELSE 0 END) = 0
        ORDER BY total DESC
    """)
    if not class_rows:
        return []
    return [{
        "type": "hueco",
        "severity": "warn",
        "count": len(class_rows),
        "classes": [{"problem_class": r["problem_class"], "total": r["total"]}
                    for r in class_rows],
        "description": (
            f"{len(class_rows)} problem_class(es) sin ningún átomo 'used' — "
            "el patrón está catalogado pero nunca se aplicó en producción."
        ),
    }]


def read_auditoria(repo: Path | None = None) -> dict:
    """Detecta problemas en el store de átomos (mem_type='fact') — calidad del catálogo.

    Delega cada tipo de hallazgo a una función privada (CC baja por función).
    Hallazgos detectados:
    - duplicados: structural_signature en >1 fila.
    - sin_validity: átomos sin validity_domain documentado.
    - sin_source: átomos sin source_project (origen desconocido).
    - bajo_valor: validity_domain con tag [BAJO VALOR] o [DESCARTADO].
    - huecos: problem_class sin ninguna instancia 'used' (patrón nunca aplicado).

    Args:
        repo: Raíz del repo ARIS4U (default DEFAULT_REPO).

    Returns:
        Dict con ``available``, ``findings`` (lista de hallazgos), ``summary``
        (conteos por tipo), ``total_findings``, ``total_atoms``.
        ``available=False`` si no hay DB (fail-soft).
    """
    repo = repo or DEFAULT_REPO
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return {"available": False, "reason": f"no se encontró {_DB}"}
    try:
        total_atoms = _scalar(conn, "SELECT count(*) FROM decisions WHERE mem_type='fact'")
        findings: list[dict] = (
            _audit_duplicados(conn)
            + _audit_sin_validity(conn)
            + _audit_sin_source(conn)
            + _audit_bajo_valor(conn)
            + _audit_huecos(conn)
        )
        summary: dict[str, int] = {}
        for f in findings:
            summary[f["type"]] = summary.get(f["type"], 0) + 1
        return {
            "available": True,
            "findings": findings,
            "summary": summary,
            "total_findings": len(findings),
            "total_atoms": total_atoms,
        }
    finally:
        conn.close()


def _by_source(conn: sqlite3.Connection) -> list[dict]:
    """Proyectos por ORIGEN: átomos globales etiquetados con source_project (fase 2: origen ≠
    scope). Aparecen como su proyecto de origen SIN perder su alcance global. [] si no hay columna."""
    out: list[dict] = []
    try:
        for r in _rows(conn, "SELECT source_project sp, count(*) n FROM decisions "
                             "WHERE source_project IS NOT NULL AND source_project!='' "
                             "GROUP BY sp ORDER BY n DESC"):
            out.append({"source": r["sp"], "label": project_label(r["sp"]),
                        "about": project_about(r["sp"]), "decisions": r["n"]})
    except sqlite3.Error:
        return []  # columna source_project aún no existe → degrada limpio
    return out


def read_memory(repo: Path | None = None, *, recent: int = 12,
                events: list[dict] | None = None) -> dict:
    """Lee el estado vivo de la memoria de ARIS4U (sessions.db + vectores + recall).

    Args:
        repo: Raíz del repo ARIS4U (default DEFAULT_REPO).
        recent: Cuántas decisiones/guards/digests recientes traer.
        events: Eventos recientes (para el medidor de recall); si None, se leen del log.

    Returns:
        Dict listo para la pantalla: totales, por-cliente, recientes y medidor de recall.
        ``available=False`` si la DB no existe (modo offline / repo movido).
    """
    repo = repo or DEFAULT_REPO
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return {"available": False, "reason": f"no se encontró {_DB}"}
    if events is None:
        events = parse_events(tail_lines(repo / _EVENTS, 400))
    try:
        def _safe_count(c: "sqlite3.Connection", tbl: str) -> int:
            """COUNT(*) en ``tbl``; devuelve 0 si la tabla no existe (fail-open)."""
            try:
                return _scalar(c, f"SELECT count(*) FROM {tbl}")
            except Exception:
                return 0

        totals = {
            "decisions": _scalar(conn, f"SELECT count(*) FROM decisions WHERE {_REAL}"),
            "guards": _scalar(conn, "SELECT count(*) FROM guards"),
            "digests": _scalar(conn, "SELECT count(*) FROM digests"),
            "vectors": _vectors_count(repo),
            "observations_local": _safe_count(conn, "observations_local"),
            "cowork_comments": _safe_count(conn, "cowork_comments"),
        }
        # por cliente: une las 3 tablas (decisions excluye provenance/git-commits)
        clients: dict[str, dict] = {}
        for tbl, key in (("decisions", "decisions"), ("guards", "guards"),
                         ("digests", "digests")):
            for cli, n in _by_client(conn, tbl, _REAL if tbl == "decisions" else "").items():
                clients.setdefault(cli, {"client": cli, "decisions": 0,
                                         "guards": 0, "digests": 0})[key] = n
        # (none) al final; el resto por nº de decisiones desc. Enriquecer con label + about.
        by_client = sorted(clients.values(),
                           key=lambda c: (c["client"] == "(none)", -c["decisions"]))
        for c in by_client:
            c["label"] = project_label(c["client"])
            c["about"] = project_about(c["client"])
        # FIX #4 (round 2): excluir provenance/fact — mismas ~1011 filas que contaminaban
        # los "últimos 12" antes del filtro _REAL.
        recent_decisions = _rows(
            conn, f"SELECT decision, domain, COALESCE(client_id,'(none)') client, "
                  f"locked, created_at FROM decisions WHERE {_REAL} "
                  f"ORDER BY created_at DESC LIMIT ?", (recent,))
        recent_guards = _rows(
            conn, "SELECT pattern, prevention, severity, COALESCE(client_id,'(none)') client, "
                  "created_at FROM guards ORDER BY created_at DESC LIMIT ?", (recent,))
        recent_digests = _rows(
            conn, "SELECT date, substr(summary,1,200) summary, COALESCE(client_id,'(none)') "
                  "client, created_at FROM digests ORDER BY date DESC, created_at DESC "
                  "LIMIT ?", (recent,))
        return {
            "available": True,
            "totals": totals,
            "by_client": by_client,
            "by_source": _by_source(conn),
            "recent_decisions": recent_decisions,
            "recent_guards": recent_guards,
            "recent_digests": recent_digests,
            "recall": recall_stats(conn, events or []),
        }
    finally:
        conn.close()


# --- A1b: Navegador de memoria (búsqueda + filtros por proyecto) ----------------------

def _iso_cutoff(days: int) -> str:
    """Fecha ISO (YYYY-MM-DD) de hace ``days`` días — para filtrar por antigüedad (stale)."""
    return (date.today() - timedelta(days=days)).isoformat()


def memory_facets(repo: Path | None = None) -> dict:
    """Clientes/proyectos y dominios distintos en la memoria (para los selects del navegador)."""
    repo = repo or DEFAULT_REPO
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return {"available": False, "clients": [], "domains": [], "types": []}
    try:
        clients = [r[0] for r in conn.execute(
            "SELECT DISTINCT COALESCE(NULLIF(client_id,''),'(none)') c FROM decisions ORDER BY c")]
        domains = [r[0] for r in conn.execute(
            "SELECT DISTINCT domain FROM decisions "
            f"WHERE domain IS NOT NULL AND domain!='' AND {_REAL} ORDER BY domain")]
        # Tipos navegables de decisión (excluye 'provenance'/'fact', internos del motor).
        types = [r[0] for r in conn.execute(
            "SELECT DISTINCT mem_type FROM decisions "
            "WHERE mem_type IS NOT NULL AND mem_type NOT IN ('provenance','fact') ORDER BY mem_type")]
        return {"available": True, "clients": clients, "domains": domains, "types": types}
    finally:
        conn.close()


def _client_clause(client: str, params: list) -> str:
    """Cláusula WHERE para un cliente exacto ('(none)' = sin cliente). Muta ``params``."""
    if client == "(none)":
        return "(client_id IS NULL OR client_id='')"
    params.append(client)
    return "client_id=?"


def _search_decisions(conn: sqlite3.Connection, *, q: str, client: str, domain: str,
                      locked: bool, stale_days: int, limit: int,
                      mem_type: str = "") -> list[dict]:
    """Decisiones que casan los filtros (texto/cliente/dominio/locked/antigüedad/mem_type)."""
    where, params = ["1=1"], []
    if q:
        where.append("(LOWER(decision) LIKE ? OR LOWER(COALESCE(domain,'')) LIKE ?)")
        params += [f"%{q.lower()}%", f"%{q.lower()}%"]
    if client:
        where.append(_client_clause(client, params))
    if domain:
        where.append("domain=?")
        params.append(domain)
    if locked:
        where.append("locked=1")
    if stale_days > 0:
        where.append("created_at < ?")
        params.append(_iso_cutoff(stale_days))
    if mem_type:
        # Filtro explícito por tipo (p.ej. 'fact', 'rule'); anula el filtro _REAL por diseño.
        where.append("mem_type=?")
        params.append(mem_type)
    else:
        # FIX #1: por defecto excluir provenance/fact (solo decisiones reales).
        # El docstring de search_memory dice "'' = sin filtro (todos los tipos excepto
        # provenance/fact que ya filtra _REAL)" — pero antes no se aplicaba el filtro.
        where.append(_REAL)
    params.append(limit)
    sql = ("SELECT decision, domain, COALESCE(NULLIF(client_id,''),'(none)') client, "
           "locked, created_at, COALESCE(mem_type,'(none)') mem_type FROM decisions WHERE "
           + " AND ".join(where) + " ORDER BY created_at DESC LIMIT ?")
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _search_guards(conn: sqlite3.Connection, *, q: str, client: str, limit: int) -> list[dict]:
    """Guards que casan texto/cliente (los guards no tienen dominio ni locked)."""
    where, params = ["1=1"], []
    if q:
        where.append("(LOWER(pattern) LIKE ? OR LOWER(COALESCE(prevention,'')) LIKE ?)")
        params += [f"%{q.lower()}%", f"%{q.lower()}%"]
    if client:
        where.append(_client_clause(client, params))
    params.append(limit)
    sql = ("SELECT pattern, prevention, severity, "
           "COALESCE(NULLIF(client_id,''),'(none)') client, created_at FROM guards WHERE "
           + " AND ".join(where) + " ORDER BY created_at DESC LIMIT ?")
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def search_memory(repo: Path | None = None, *, q: str = "", client: str = "",
                  domain: str = "", locked: bool = False, stale_days: int = 0,
                  limit: int = 80, mem_type: str = "") -> dict:
    """Busca decisiones (+guards) con filtros — el navegador de memoria por proyecto.

    Args:
        repo: Raíz del repo ARIS4U.
        q: Texto a buscar (LIKE, case-insensitive) en decisión/dominio y pattern/prevención.
        client: Cliente/proyecto exacto ('(none)' = sin cliente; '' = todos).
        domain: Dominio exacto ('' = todos). Solo aplica a decisiones.
        locked: Solo decisiones locked=1.
        stale_days: Solo filas anteriores a hoy-stale_days (0 = sin filtro de antigüedad).
        limit: Máximo de filas por tipo.
        mem_type: Filtro opcional por mem_type (e.g. 'rule', 'episode', 'decision', 'fact').
                  '' = sin filtro (todos los tipos excepto provenance/fact que ya filtra _REAL).

    Returns:
        Dict con ``decisions`` + ``guards`` casados + ``count`` total + ``mem_type_counts``.
        Los guards se omiten cuando se filtra por dominio o locked (no aplican a esa tabla).
    """
    repo = repo or DEFAULT_REPO
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return {"available": False, "reason": f"no se encontró {_DB}"}
    try:
        decisions = _search_decisions(conn, q=q, client=client, domain=domain,
                                      locked=locked, stale_days=stale_days, limit=limit,
                                      mem_type=mem_type)
        guards = ([] if (domain or locked)
                  else _search_guards(conn, q=q, client=client, limit=limit))
        # Conteos por mem_type para el facet del front (siempre desde la tabla completa)
        mem_type_counts: dict[str, int] = {}
        for row in conn.execute(
                "SELECT COALESCE(mem_type,'(none)') t, COUNT(*) n FROM decisions "
                "GROUP BY t ORDER BY n DESC"):
            mem_type_counts[row[0]] = row[1]
        return {"available": True, "decisions": decisions, "guards": guards,
                "count": len(decisions) + len(guards),
                "mem_type_counts": mem_type_counts,
                "mem_type_filter": mem_type or ""}
    finally:
        conn.close()


# --- A1c: Panel de deuda técnica (gate_results) ---------------------------------------

@lru_cache(maxsize=4)
def _repo_python_files(repo: Path) -> frozenset[str]:
    """Basenames de ficheros .py del repo, excluyendo dirs de entorno. Cacheado por proceso.

    ``maxsize=4`` cubre el caso de trabajar en dos repos distintos. La caché se invalida en
    ``/regenerate`` con ``_repo_python_files.cache_clear()`` (igual que ``_project_profile``).
    PERF fix #5 (7º gate adversarial 2026-06-29): antes ``read_quality`` hacía ``rglob`` del
    repo entero en cada GET /quality sin caché — costoso en repos grandes.
    """
    _skip = (".venv312", ".venv", ".git", "__pycache__", "node_modules")
    return frozenset(p.name for p in repo.rglob("*.py")
                     if not any(d in p.parts for d in _skip))


def _quality_module_row(r: dict, repo_files: frozenset[str]) -> dict:
    """Fila de calidad de un módulo (tasa-limpia, tipo, flags) desde su agregado de gate_results.

    ``repo_files`` = nombres de archivo .py que existen en el repo del motor, para marcar
    ``in_repo``: el panel mezcla módulos de VARIOS proyectos (el gate escribe en una DB global),
    así que distinguir "motor" de "externo" (cliente/otro) evita leer deuda ajena como del motor.

    LIMITACIÓN CONOCIDA: ``gate_results.module_name`` almacena solo el basename del archivo
    (``os.path.basename(file_path)``, ver code_quality_gate.py). La comprobación ``in_repo``
    usa basename-a-basename, por lo que un ``models.py`` de un proyecto cliente contaría como
    módulo del motor si el motor también tiene un ``models.py``. Colisiones raras en la práctica
    (nombres de módulo ARIS4U son específicos), pero posibles. Fix real = almacenar path relativo
    al proyecto en gate_results; cambio de esquema pendiente.
    """
    tot = r["total"] or 0
    cln = r["clean"] or 0
    name = str(r["module_name"])
    is_commit = name.startswith("commit:")
    return {
        "module_name": r["module_name"],
        "kind": "commit" if is_commit else "module",
        "in_repo": (not is_commit) and name in repo_files,
        "total": tot,
        "clean": cln,
        "issues": r["issues"] or 0,
        "clean_rate": round(100.0 * cln / tot, 1) if tot else 0.0,
        "never_clean": cln == 0,
        "last_status": r["last_status"] or "",
        "last_ts": r["last_ts"] or "",
    }


def _quality_totals(mods: list[dict], total: int, clean: int, issues_total: int) -> dict:
    """Conteos del summary de Calidad, separando deuda del MOTOR de la de OTROS proyectos.

    El gate escribe en una DB global (todos los proyectos); separar ``in_repo`` (motor) de lo
    ajeno evita leer deuda de clientes/labs como deuda del motor. ``0 del motor`` = estado-objetivo.
    """
    dirty = sum(1 for m in mods if m["last_status"] == "issues")
    dirty_motor = sum(1 for m in mods if m["last_status"] == "issues" and m["in_repo"])
    return {
        "total": total, "clean": clean, "issues": issues_total,
        "modules_dirty_now": dirty,
        "modules_never_clean": sum(1 for m in mods if m["never_clean"]),
        "modules_dirty_motor": dirty_motor,
        "modules_dirty_otros": dirty - dirty_motor,
    }


def read_quality(repo: Path | None = None, *, top_n: int = 15) -> dict:
    """Panel de deuda técnica: agrega gate_results por módulo y surfacea los más problemáticos.

    Lee la tabla ``gate_results`` de ``data/sessions.db`` (columnas: module_name, status
    [clean|issues], details, timestamp) y devuelve un ranking de los módulos con más fallos
    junto con totales globales — útil para decidir qué arreglar primero.

    Args:
        repo: Raíz del repo ARIS4U (default DEFAULT_REPO).
        top_n: Cuántos módulos peores incluir en ``top_issues`` (default 15).

    Returns:
        Dict con:
          - available (bool)
          - totals: {total, clean, issues}
          - last_gate: timestamp ISO del gate más reciente (o "" si no hay)
          - top_issues: list[{module_name, total, clean, issues, last_status, last_ts}]
            ordenada por issues DESC, limitada a top_n
    """
    repo = repo or DEFAULT_REPO
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return {"available": False, "reason": f"no se encontró {_DB}"}
    try:
        total = _scalar(conn, "SELECT COUNT(*) FROM gate_results")
        if total == 0:
            return {"available": True, "totals": {"total": 0, "clean": 0, "issues": 0},
                    "last_gate": "", "top_issues": []}
        clean = _scalar(conn, "SELECT COUNT(*) FROM gate_results WHERE status='clean'")
        issues_total = total - clean
        last_gate_row = conn.execute(
            "SELECT MAX(timestamp) FROM gate_results").fetchone()
        last_gate: str = last_gate_row[0] if last_gate_row and last_gate_row[0] else ""
        # Agregado por módulo + last_status (subconsulta correlacionada, un solo query).
        rows = _rows(conn,
            "SELECT g.module_name, "
            "  COUNT(*) AS total, "
            "  SUM(CASE WHEN g.status='clean' THEN 1 ELSE 0 END) AS clean, "
            "  SUM(CASE WHEN g.status='issues' THEN 1 ELSE 0 END) AS issues, "
            "  MAX(g.timestamp) AS last_ts, "
            "  (SELECT s.status FROM gate_results s WHERE s.module_name=g.module_name "
            "   ORDER BY s.timestamp DESC LIMIT 1) AS last_status "
            "FROM gate_results g GROUP BY g.module_name")
        repo_files = _repo_python_files(repo)
        mods = [_quality_module_row(r, repo_files) for r in rows]
        # Ranking ACCIONABLE: primero lo que FALLA AHORA (last_status='issues'), luego peor
        # tasa-limpia, luego más issues. NO por conteo histórico (confunde 'ruidoso pero
        # limpio ahora' con 'roto ahora').
        mods.sort(key=lambda m: (m["last_status"] != "issues", m["clean_rate"], -m["issues"]))
        return {
            "available": True,
            "totals": _quality_totals(mods, total, clean, issues_total),
            "last_gate": last_gate,
            "top_issues": mods[:top_n],
        }
    finally:
        conn.close()


# --- A1d: Session briefs de observations_local (V18 Fase E: texto propio, no claude-mem) ---


def read_session_briefs(limit: int = 15, repo: Path | None = None) -> dict:
    """Últimas observaciones PROPIAS (sessions.db/observations_local) — ceba sesiones rápido.

    V18 Fase E (desacople de claude-mem, 2026-07-02): lee la tabla PROPIA `observations_local`
    en `data/sessions.db`, no la claude-mem.db 3er-party (RETIRADA/archivada). Columnas:
    id, project, content, type, created_at. Ordena por rowid DESC (=inserción; created_at
    puede ser NULL en filas migradas). Acotado con LIMIT, nunca recorre toda la DB.

    Args:
        limit: Máximo de briefs a devolver (default 15, max 50).
        repo: Raíz del repo ARIS4U (default DEFAULT_REPO).

    Returns:
        Dict con available, total_in_db, briefs: list[{id, project, request_short,
        learned_short, completed_short, created_at}].
    """
    safe_limit = max(1, min(50, limit))
    repo = repo or DEFAULT_REPO
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return {"available": False, "reason": "sessions.db no encontrada o inaccesible"}
    try:
        total = _scalar(conn, "SELECT COUNT(*) FROM observations_local")
        rows = _rows(conn,
            "SELECT id, project, content, type, created_at "
            "FROM observations_local "
            "ORDER BY rowid DESC "
            # safe_limit es int acotado; el f-string es seguro (sin input de usuario).
            f"LIMIT {safe_limit}")
        briefs: list[dict] = []
        for r in rows:
            content = (r["content"] or "").strip()
            briefs.append({
                "id": r["id"],
                "project": r["project"] or "",
                "request_short": content[:120],
                "learned_short": content[:200],
                "completed_short": (r["type"] or "")[:120],
                "created_at": r["created_at"] or "",
            })
        return {"available": True, "total_in_db": total, "briefs": briefs}
    finally:
        conn.close()


# --- A0: Tablero de ESTADO (verde/naranja/rojo de cada parte) -------------------------

def _port_open(port: int, timeout: float = 0.5) -> bool:
    """True si algo acepta conexión en 127.0.0.1:port ahora (probe rápido)."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _days_since(iso: str) -> int | None:
    """Días desde una fecha ISO (toma los primeros 10 chars); None si no parsea."""
    try:
        return (date.today() - date.fromisoformat(str(iso)[:10])).days
    except (ValueError, TypeError):
        return None


def _ddmmyyyy(iso: str) -> str:
    """Fecha ISO → 'DD/MM/AAAA' (o '' si no parsea)."""
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d/%m/%Y")
    except (ValueError, TypeError):
        return ""


def _file_mdate(path: Path) -> str:
    """Fecha de última modificación de un archivo, 'DD/MM/AAAA' (o '')."""
    from datetime import datetime
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%d/%m/%Y")
    except OSError:
        return ""


# Para-qué (explicación breve, lenguaje de no-programador) de cada parte.
_PURPOSE = {
    "Memoria": "Que ARIS4U recuerde lo que decidimos, entre sesiones.",
    "Búsqueda semántica": "Encontrar memoria por significado, no solo por palabra exacta.",
    "Recall útil": "Que la memoria que trae de vuelta de verdad sirva.",
    "Herramientas MCP": "Los brazos con que ARIS4U actúa: buscar, guardar, criticar…",
    "Hooks (reflejos)": "Los automatismos que corren solos en cada paso de Claude.",
    "Amplificador": "El cuerpo local que potencia a Claude; a 30 etiquetas se autocalibra.",
    "Cuerpo local (MLX)": "Modelo local que estructura y critica para amplificar.",
    "Ollama (local)": "Modelos locales (embeddings) sin enviar datos afuera.",
}


def _si(name: str, status: str, metric: str, value: float, maxv: float,
        kind: str = "ratio", updated: str = "") -> dict:
    """Item de estado uniforme. ``kind``: 'ratio' (X/Y) · 'count' (número) · 'state' (on/off).
    ``updated`` = fecha DD/MM/AAAA del último cambio de ese dato ('' = en vivo). ``status`` ∈ ok/warn/down."""
    pct = round(value / maxv * 100) if maxv else (100 if status == "ok" else 0)
    return {"name": name, "status": status, "metric": metric, "value": value, "max": maxv,
            "pct": min(pct, 100), "kind": kind, "updated": updated, "purpose": _PURPOSE.get(name, "")}


def _freshness_status(days: int | None) -> str:
    """Madurez por antigüedad: ≤2d ok · ≤14d (o sin fecha) warn · resto down."""
    if days is not None and days <= 2:
        return "ok"
    return "warn" if (days is None or days <= 14) else "down"


def _st_memory(repo: Path) -> dict:
    """¿La memoria se está escribiendo? (write-path: el modo de fallo #1, silencioso)."""
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return _si("Memoria", "down", "sin DB", 0, 1, "state")
    try:
        n = _scalar(conn, f"SELECT count(*) FROM decisions WHERE {_REAL}")
        # FIX #4: MAX sobre decisiones REAL únicamente; sin el filtro, las filas provenance
        # (git-commits, etc.) adelantan la fecha → la "última actividad" miente.
        row = conn.execute(f"SELECT MAX(created_at) FROM decisions WHERE {_REAL}").fetchone()
        last = row[0] if row and row[0] else None
        st = _freshness_status(_days_since(last) if last else None)
        return _si("Memoria", st, f"{n} decisiones", n, n or 1, "count", _ddmmyyyy(last) if last else "")
    finally:
        conn.close()


def _st_vectors(repo: Path) -> dict:
    """¿La búsqueda semántica tiene cuerpo (vectores)?"""
    n = _vectors_count(repo)
    st = "ok" if n > 100 else ("warn" if n > 0 else "down")
    return _si("Búsqueda semántica", st, f"{n} vectores", n, n or 1, "count", _file_mdate(repo / _VEC))


def _st_recall(repo: Path) -> dict:
    """¿El recall recibe señal de utilidad? (sin señal = no se puede medir/mejorar)."""
    conn = _connect_ro(repo / _DB)
    if conn is None:
        return _si("Recall útil", "warn", "sin datos", 0, 1, "state")
    try:
        total = _scalar(conn, "SELECT count(*) FROM recall_feedback")
        useful = _scalar(conn, "SELECT count(*) FROM recall_feedback WHERE useful=1")
        if total == 0:
            return _si("Recall útil", "warn", "sin señal aún", 0, 1, "state")
        st = "ok" if useful / total >= 0.5 else "warn"
        # recall_feedback schema: recall_id, useful, marked_at, method, score, detail
        # (no created_at column — use marked_at which always exists)
        try:
            row = conn.execute("SELECT MAX(marked_at) FROM recall_feedback").fetchone()
            upd = _ddmmyyyy(row[0]) if row and row[0] else ""
        except sqlite3.Error:
            upd = ""
        return _si("Recall útil", st, f"{useful}/{total} útiles", useful, total, "ratio", upd)
    finally:
        conn.close()


def _st_mcp(repo: Path) -> dict:
    """¿Los brazos MCP (las 7 herramientas) están disponibles?"""
    import re
    server = repo / "integrations" / "mcp_server.py"
    if not server.is_file():
        return _si("Herramientas MCP", "down", "0/7 tools", 0, 7, "ratio")
    n = len(re.findall(r"@mcp\.tool\(\)", server.read_text(encoding="utf-8", errors="ignore")))
    return _si("Herramientas MCP", "ok" if n >= 7 else "warn", f"{n}/7 tools", n, 7, "ratio",
               _file_mdate(server))


def _st_hooks(repo: Path) -> dict:
    """¿Los hooks (los reflejos automáticos) están cableados?

    P2-B: reporta "N de M eventos cableados" sobre el total de _LIFECYCLE (9) con badge
    ``warn`` cuando algún evento del ciclo de vida queda sin cablear — en vez de mostrar
    ``ok`` engañoso al llegar a 7/9 (que ocultaba SubagentStop y PreCompact sin cablear).
    """
    hj = repo / _HOOKS_JSON
    if not hj.is_file():
        return _si("Hooks (reflejos)", "down", "0 eventos", 0, 1, "count", _file_mdate(hj))
    try:
        obj = json.loads(hj.read_text(encoding="utf-8"))
        ev = obj.get("hooks", obj)
        n = len(ev) if isinstance(ev, dict) else 0
        # FIX #6: usar MAX(mtime hooks.json, mtime settings.json) — settings.json contiene
        # los +handlers y su mtime (26/06) es más reciente que hooks.json (11/06).
        settings_json = Path.home() / ".claude" / "settings.json"
        hj_mtime = hj.stat().st_mtime if hj.is_file() else 0.0
        sj_mtime = settings_json.stat().st_mtime if settings_json.is_file() else 0.0
        updated = _file_mdate(hj if hj_mtime >= sj_mtime else settings_json)
        total = len(_LIFECYCLE)  # P2-B: total canónico (9 eventos del ciclo de vida)
        # warn si faltan eventos por cablear; down si no hay ninguno cableado.
        if n == 0:
            status = "down"
        elif n < total:
            status = "warn"
        else:
            status = "ok"
        return _si("Hooks (reflejos)", status, f"{n} de {total} eventos cableados",
                   n, total, "ratio", updated)
    except (OSError, ValueError):
        return _si("Hooks (reflejos)", "warn", "ilegible", 0, 1, "state")


def _st_body() -> dict:
    """¿El cuerpo local (MLX)? Apagado = STANDBY por diseño (se enciende al amplificar), NO un fallo."""
    up = _mlx_up()
    return _si("Cuerpo local (MLX)", "ok",
               "encendido" if up else "standby (apagado)", 1 if up else 0, 1, "state")


def _st_ollama() -> dict:
    """¿Ollama (embeddings/modelos locales) responde? Caído SÍ es un problema (la semántica lo usa)."""
    up = _port_open(11434)
    return _si("Ollama (local)", "ok" if up else "warn",
               "responde" if up else "no responde", 1 if up else 0, 1, "state")


def _st_amplifier(repo: Path) -> dict:
    """¿El lazo del amplificador? Acumular hacia la calibración (30) es un estado SANO y en
    progreso (necesita uso real), NO un fallo → verde con su progreso visible."""
    a = read_amplifier(repo)
    if not a.get("available"):
        return _si("Amplificador", "warn", "sin datos", 0, 30, "state")
    lab, thr = a.get("labeled", 0), a.get("threshold", 30)
    metric = "calibrado ✓" if a.get("ready_for_calibration") else f"{lab}/{thr} en progreso"
    return _si("Amplificador", "ok", metric, lab, thr, "ratio", _file_mdate(repo / _EVENTS))


# Mapa función → nombre humano para el fallback de read_status (Fix #1).
# Mantiene el contrato del espejo aunque la función lance: el nombre en el item
# siempre es el nombre legible (igual al que la función devolvería en condiciones normales),
# no el __name__ técnico ('_st_memory', etc.).
_ST_HUMAN: dict = {
    _st_memory:    "Memoria",
    _st_vectors:   "Búsqueda semántica",
    _st_recall:    "Recall útil",
    _st_mcp:       "Herramientas MCP",
    _st_hooks:     "Hooks (reflejos)",
    _st_amplifier: "Amplificador",
    _st_body:      "Cuerpo local (MLX)",
    _st_ollama:    "Ollama (local)",
}


def read_status(repo: Path | None = None) -> dict:
    """Tablero de salud: cada parte de ARIS4U en verde (ok) / naranja (warn) / rojo (down).

    Pensado para DOS consumidores: el humano lo ve como semáforos; Claude lo consulta de un
    tiro (vía la MCP/endpoint) en vez de inspeccionar a mano. Todo read-only y fail-soft.

    Returns:
        Dict con ``items`` (name/status/detail), ``summary`` (conteos) y ``overall``.
    """
    repo = repo or DEFAULT_REPO
    items: list[dict] = []
    for fn in (_st_memory, _st_vectors, _st_recall, _st_mcp, _st_hooks, _st_amplifier):
        try:
            items.append(fn(repo))
        except Exception:
            items.append(_si(_ST_HUMAN[fn], "warn", "no se pudo evaluar", 0, 1, "state"))
    for fn0 in (_st_body, _st_ollama):
        try:
            items.append(fn0())
        except Exception:
            items.append(_si(_ST_HUMAN[fn0], "warn", "no se pudo evaluar", 0, 1, "state"))
    c = Counter(i["status"] for i in items)
    overall = "down" if c.get("down") else ("warn" if c.get("warn") else "ok")
    return {"available": True, "items": items, "overall": overall,
            "summary": {"ok": c.get("ok", 0), "warn": c.get("warn", 0), "down": c.get("down", 0)}}


# --- A2: Telemetría -------------------------------------------------------------------

# Campos salientes por tipo de evento (para el resumen de una línea).
_EVENT_SALIENT = {
    "auto_recall": ("query", "n_semantic", "latency_ms"),
    "model_hint": ("intent", "model", "confidence"),
    "model_route": ("intent", "model"),
    "depth_inject": ("intent", "latency_ms"),
    "mcp_tool": ("tool", "latency_ms"),
    "subagent_start": ("agent_type", "subagent_type"),
    "agent_output_verified": ("agent_id", "ok"),
    "migration_lint_blocked": ("file", "reason"),
    "migration_finding": ("file", "severity"),
    "phi_detected": ("where", "kind"),
    "phi_to_external_blocked": ("target",),
    "secret_redacted": ("kind",),
    "novelty_check": ("novelty", "intent"),
    "session_briefing": ("client",),
}


# Nombre humano + para-qué-sirve de cada evento (para que el Pulso dé VALOR, no ruido técnico).
_EVENT_HUMAN = {
    "auto_recall": ("🧠 Recall de memoria", "ARIS4U buscó en su memoria contexto útil para tu mensaje"),
    "model_hint": ("🎯 Sugerencia de modelo", "Eligió qué modelo conviene según la dificultad de la tarea"),
    "model_route": ("🔀 Ruteo de modelo", "Envió el trabajo al modelo elegido"),
    "depth_inject": ("📏 Profundidad", "Ajustó cuánto debe razonar Claude según el tipo de tarea"),
    "mcp_tool": ("🔭 Herramienta MCP", "Se usó una de las herramientas de ARIS4U"),
    "subagent_start": ("🤖 Sub-agente lanzado", "Claude delegó trabajo a un agente en paralelo"),
    "agent_dispatched": ("🤖 Sub-agente despachado", "Se envió una subtarea a un agente"),
    "agent_output_verified": ("✅ Verificación de agente", "Se revisó que el resultado del agente sea válido"),
    "agent_verify_no_changes": ("✅ Agente sin cambios", "El agente terminó sin modificar archivos"),
    "migration_lint_blocked": ("🛡️ Migración bloqueada", "Un guard frenó una migración de base de datos riesgosa"),
    "migration_finding": ("🔎 Hallazgo en migración", "Se detectó algo a revisar en una migración"),
    "phi_detected": ("🏥 Dato sensible detectado", "Se vio información médica/PHI y se protegió"),
    "phi_to_external_blocked": ("🏥 PHI bloqueado", "Se impidió enviar datos sensibles fuera"),
    "secret_redacted": ("🔒 Secreto ocultado", "Se ocultó una credencial para que no se filtre"),
    "novelty_check": ("✨ Chequeo de novedad", "Evaluó si algo es nuevo o ya se conocía"),
    "session_briefing": ("📋 Briefing de sesión", "ARIS4U cargó el contexto al empezar la sesión"),
    "capture_commit": ("💾 Commit capturado", "Registró un commit de git en la memoria"),
    "code_quality_gate": ("🧪 Control de calidad", "Revisó la calidad del código que se editó"),
    "commit_quality_gate": ("🧪 Calidad de commit", "Revisó la calidad antes de un commit"),
}


def event_human(etype: str) -> tuple[str, str]:
    """(nombre humano, para-qué) de un tipo de evento; fallback legible si no está mapeado."""
    if etype in _EVENT_HUMAN:
        return _EVENT_HUMAN[etype]
    return ("⚙ " + etype.replace("_", " ").capitalize(), "Actividad interna de ARIS4U")


_SKIP_KEYS = ("event", "hook", "ts", "session_id")


def _fmt_fields(e: dict, keys: tuple[str, ...]) -> list[str]:
    """``k=v`` para cada clave presente y no vacía."""
    return [f"{k}={e[k]}" for k in keys if e.get(k) not in ("", None)]


def event_summary(e: dict) -> str:
    """Resumen de una línea de un evento: campos salientes por tipo, o las primeras claves."""
    etype = e.get("event") or e.get("hook") or "?"
    keys = _EVENT_SALIENT.get(etype) or tuple(k for k in e if k not in _SKIP_KEYS)[:3]
    return " · ".join(p[:60] for p in _fmt_fields(e, keys))


def _recent_event(e: dict) -> dict:
    """Un evento listo para la pantalla: tipo + nombre humano + para-qué + resumen técnico."""
    etype = e.get("event") or e.get("hook") or "?"
    label, desc = event_human(etype)
    return {"ts": e.get("ts", ""), "type": etype, "hook": e.get("hook", ""),
            "label": label, "desc": desc, "summary": event_summary(e)}


def read_telemetry(repo: Path | None = None, *, limit: int = 60,
                   window: int = 1500) -> dict:
    """Lee el flujo de telemetría: eventos recientes + agregados por tipo sobre una ventana.

    Args:
        repo: Raíz del repo ARIS4U.
        limit: Cuántos eventos recientes devolver (con resumen).
        window: Cuántas líneas leer del final del log para los agregados.

    Returns:
        Dict con eventos recientes, conteo por tipo, por hook y total leído.
    """
    repo = repo or DEFAULT_REPO
    path = repo / _EVENTS
    if not path.is_file():
        return {"available": False, "reason": f"no se encontró {_EVENTS}"}
    events = parse_events(tail_lines(path, window))
    by_type = Counter(e.get("event") or e.get("hook") or "?" for e in events)
    by_hook = Counter(e.get("hook") for e in events if e.get("hook"))
    recent = [_recent_event(e) for e in events[-limit:]][::-1]  # más nuevo primero
    # NO devolvemos los eventos crudos: contienen el prompt verbatim (query) y el texto íntegro
    # de la memoria recuperada (injected) — datos sensibles + ~450KB de peso muerto. Solo el
    # resumen truncado (recent) + agregados. read_hooks lee su propia ventana del log.
    return {
        "available": True,
        "window": len(events),
        "by_type": dict(by_type.most_common()),
        "by_hook": dict(by_hook.most_common()),
        "recent": recent,
    }


# --- A2b: phi_guard block counter -----------------------------------------------------

def read_phi_guard_blocks(repo: Path | None = None) -> dict:
    """Cuenta los bloqueos de phi_guard registrados en logs/v16.1-events.jsonl.

    Busca eventos con ``hook='phi_guard'`` y ``event='phi_to_external_blocked'`` — la
    señal exacta que phi_guard.py escribe al bloquear (ver phi_guard.py:174). Fail-soft:
    devuelve ``{available: False}`` si el log no existe.

    Args:
        repo: Raíz del repo ARIS4U (default DEFAULT_REPO).

    Returns:
        Dict con ``available``, ``total`` (nº de bloqueos encontrados en el log completo),
        ``last_ts`` (ISO timestamp del más reciente, o ''), ``last_tool`` (herramienta
        bloqueada más reciente, o '').
    """
    repo = repo or DEFAULT_REPO
    path = repo / _EVENTS
    if not path.is_file():
        return {"available": False, "reason": f"no se encontró {_EVENTS}"}
    total = 0
    last_ts = ""
    last_tool = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw or '"phi_guard"' not in raw:
                    continue
                try:
                    ev = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if (ev.get("hook") == "phi_guard"
                        and ev.get("event") == "phi_to_external_blocked"):
                    total += 1
                    ts = ev.get("ts", "")
                    if ts > last_ts:
                        last_ts = ts
                        last_tool = ev.get("tool", "")
    except OSError:
        return {"available": False, "reason": "error leyendo log"}
    return {
        "available": True,
        "total": total,
        "last_ts": last_ts,
        "last_tool": last_tool,
    }


# --- A4: Hooks / guards ---------------------------------------------------------------

def _wired_events(hooks_obj: dict) -> dict[str, list[str]]:
    """Extrae {evento: [comandos…]} de una config de hooks (hooks.json o settings.json)."""
    hooks = hooks_obj.get("hooks", hooks_obj) if isinstance(hooks_obj, dict) else {}
    out: dict[str, list[str]] = {}
    for ev, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        cmds: list[str] = []
        for g in groups:
            for h in (g.get("hooks", []) if isinstance(g, dict) else []):
                cmd = h.get("command", "")
                if cmd:
                    cmds.append(cmd)
        out[ev] = cmds
    return out


def _load_json(path: Path) -> dict:
    """Carga un JSON, {} si falla (fail-soft)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _short_cmd(cmd: str) -> str:
    """Acorta un comando de hook a su pieza identificable (script o módulo)."""
    for tok in cmd.replace("'", " ").replace('"', " ").split():
        if tok.endswith(".sh") or tok.endswith(".py") or "dispatch" in tok:
            return tok.rsplit("/", 1)[-1]
    return cmd.strip()[:48]


def _hook_rows(repo_wired: dict[str, list[str]],
               global_wired: dict[str, list[str]]) -> list[dict]:
    """Una fila por evento del ciclo de vida: comandos cableados en repo y global."""
    all_events = sorted(set(_LIFECYCLE) | set(repo_wired) | set(global_wired),
                        key=lambda x: (_LIFECYCLE.index(x) if x in _LIFECYCLE else 99, x))
    return [{
        "event": ev,
        "repo": [_short_cmd(c) for c in repo_wired.get(ev, [])],
        "global": [_short_cmd(c) for c in global_wired.get(ev, [])],
        "wired": bool(repo_wired.get(ev) or global_wired.get(ev)),
    } for ev in all_events]


_HOOKS_WINDOW_LINES = 1500  # tamaño de ventana de read_hooks — exportado para el selfcheck


def _count_fired_by_handler(
    events: list[dict],
) -> tuple[Counter[str], dict[str, str], dict[str, int], dict[str, str]]:
    """Cuenta disparos y último ts por handler y por lifecycle directo (sin campo ``hook``).

    Procesa el log de telemetría diferenciando dos rutas:
    - Eventos con ``hook``: acumulados en ``fired`` / ``last_fired`` (por handler).
    - Eventos con ``event`` mapeado en ``_EVENT_TO_LIFECYCLE`` pero sin ``hook``: acumulados en
      ``direct_lc_count`` / ``direct_lc_last`` (auto_recall, capability_hint, etc.).

    Args:
        events: Eventos de telemetría parseados (de ``parse_events``).

    Returns:
        Tuple ``(fired, last_fired, direct_lc_count, direct_lc_last)`` donde
        ``fired`` es un ``Counter`` handler→N, ``last_fired`` es handler→último-ts ISO,
        y los ``direct_lc_*`` acumulan lifecycle directo (sin "hook").
    """
    fired: Counter[str] = Counter()
    last_fired: dict[str, str] = {}
    direct_lc_count: dict[str, int] = {}
    direct_lc_last: dict[str, str] = {}
    for e in events:
        h = e.get("hook")
        ts = e.get("ts") or e.get("timestamp", "")
        ts = ts if isinstance(ts, str) else ""
        if isinstance(h, str) and h:
            fired[h] += 1
            if ts > last_fired.get(h, ""):
                last_fired[h] = ts
        else:
            ev_name = e.get("event", "")
            if isinstance(ev_name, str) and ev_name in _EVENT_TO_LIFECYCLE:
                lc = _EVENT_TO_LIFECYCLE[ev_name]
                direct_lc_count[lc] = direct_lc_count.get(lc, 0) + 1
                if ts > direct_lc_last.get(lc, ""):
                    direct_lc_last[lc] = ts
    return fired, last_fired, direct_lc_count, direct_lc_last


def _aggregate_event_counts(
    fired: Counter[str],
    last_fired: dict[str, str],
    direct_lc_count: dict[str, int],
    direct_lc_last: dict[str, str],
) -> tuple[dict[str, int], dict[str, str]]:
    """Agrega disparos de handlers y lifecycle directo en conteos por evento del ciclo de vida.

    Cruza ``fired`` con ``_HANDLER_TO_EVENT`` para sumar disparos de handler al evento de
    lifecycle correspondiente, luego mezcla los conteos directos (``_EVENT_TO_LIFECYCLE``).

    Args:
        fired: Counter handler→N (salida de ``_count_fired_by_handler``).
        last_fired: handler→último-ts ISO (salida de ``_count_fired_by_handler``).
        direct_lc_count: lifecycle→N de eventos sin campo "hook".
        direct_lc_last: lifecycle→último-ts de eventos sin campo "hook".

    Returns:
        Tupla ``(event_count, event_last)`` indexadas por nombre de lifecycle event.
    """
    event_count: dict[str, int] = {}
    event_last: dict[str, str] = {}
    for handler, count in fired.items():
        ev = _HANDLER_TO_EVENT.get(handler)
        if not ev:
            continue
        event_count[ev] = event_count.get(ev, 0) + count
        ts_h = last_fired.get(handler, "")
        if ts_h > event_last.get(ev, ""):
            event_last[ev] = ts_h
    # Mezclar conteos de lifecycle directo (_EVENT_TO_LIFECYCLE)
    for lc, count in direct_lc_count.items():
        event_count[lc] = event_count.get(lc, 0) + count
        ts_d = direct_lc_last.get(lc, "")
        if ts_d > event_last.get(lc, ""):
            event_last[lc] = ts_d
    return event_count, event_last


def read_hooks(repo: Path | None = None, *, home: Path | None = None,
               events: list[dict] | None = None) -> dict:
    """Estado de hooks/guards: qué eventos están cableados (repo + global) y cuáles dispararon.

    Cruza la telemetría (campo ``hook`` por-handler) con los eventos del ciclo de vida de Claude
    Code usando ``_HANDLER_TO_EVENT``, de modo que cada fila de evento reporta el total de
    disparos y el último timestamp de TODOS sus handlers. Los handlers sin mapeo confirmado
    (``mcp_server``, ``f1_feedback``) se conservan en ``fired_by_source`` sin asignarles evento.

    Args:
        repo: Raíz del repo ARIS4U.
        home: HOME para ~/.claude/settings.json (default Path.home()).
        events: Eventos de telemetría (de read_telemetry) para contar disparos; se releen si None.

    Returns:
        Dict con:
        - ``events``: lista de eventos del ciclo de vida; cada entrada incluye ``count`` y
          ``last_fired`` agregados de sus handlers.
          IMPORTANTE: los conteos son de la VENTANA (tail 1500 líneas del log), no all-time.
        - ``window_lines``: tamaño configurado de la ventana (FIX #5 — antes no se exponía).
        - ``window``: eventos efectivamente parseados en esa ventana.
        - ``fired_by_source``: conteo bruto handler→N (para el contenedor "otros").
        - ``last_fired``: dict handler→último-ts (legacy, para compatibilidad).
    """
    repo = repo or DEFAULT_REPO
    home = home or Path.home()
    repo_wired = _wired_events(_load_json(repo / _HOOKS_JSON))
    global_wired = _wired_events(_load_json(home / ".claude" / "settings.json"))
    _events_loaded = events is None  # True si nosotros leemos el log
    if events is None:
        events = parse_events(tail_lines(repo / _EVENTS, _HOOKS_WINDOW_LINES))

    # Conteo + último ts por handler; también acumula lifecycle directo (FIX #2).
    fired, last_fired, direct_lc_count, direct_lc_last = _count_fired_by_handler(events)
    event_count, event_last = _aggregate_event_counts(
        fired, last_fired, direct_lc_count, direct_lc_last
    )

    rows = _hook_rows(repo_wired, global_wired)
    # Enriquecer cada fila con la actividad agregada de sus handlers
    for row in rows:
        ev = row["event"]
        row["count"] = event_count.get(ev, 0)
        row["last_fired"] = event_last.get(ev, "")

    # P2-A: mapeo evento→handlers con conteo individual, para expandir en la vista.
    # Solo handlers con mapeo confirmado en _HANDLER_TO_EVENT; los sin mapeo (mcp_server,
    # f1_feedback) quedan solo en fired_by_source.
    handler_map: dict[str, list[dict]] = {}
    for handler, count in fired.most_common():
        ev = _HANDLER_TO_EVENT.get(handler)
        if not ev:
            continue
        handler_map.setdefault(ev, []).append({
            "handler": handler,
            "count": count,
            "last_fired": last_fired.get(handler, ""),
        })

    # P2-B: conteo de eventos cableados vs total de _LIFECYCLE para el badge de estado.
    wired_count = sum(1 for r in rows if r["wired"])
    total_lifecycle = len(_LIFECYCLE)

    # Sugerencias de uso para los 2 eventos no-cableados del ciclo de vida de Claude Code.
    _UNCABLED_HINTS: dict[str, str] = {
        "SubagentStop": (
            "sin cablear — posible uso: capturar métricas de subagentes al terminar "
            "(duración, modelo, éxito/fallo) para el medidor de routing"
        ),
        "PreCompact": (
            "sin cablear — posible uso: exportar/comprimir contexto antes de compactar "
            "(briefing automático o guardar el estado en memoria)"
        ),
    }

    return {
        "available": True,
        "events": rows,
        # FIX #5: exponer el tamaño de ventana para que el render muestre el número real.
        "window_lines": _HOOKS_WINDOW_LINES if _events_loaded else None,
        "window": len(events),
        "fired_by_source": dict(fired.most_common()),
        "last_fired": last_fired,
        # P2-A: handlers por evento (para el desglose expandible en la vista Hooks).
        "handler_map": handler_map,
        # P2-B: conteo real para el badge de estado.
        "wired_count": wired_count,
        "total_lifecycle": total_lifecycle,
        "uncabled_hints": {
            ev: hint
            for ev, hint in _UNCABLED_HINTS.items()
            if ev in {r["event"] for r in rows if not r["wired"]}
        },
    }


# --- Cabina del amplificador F1 (espeja tools/f1_roi.py + tools/f1_label.py del motor) ---

_F1_TOOLS = ("aris_structure", "aris_critique")
_CALIB_THRESHOLD = 30  # = _MIN_LABELS del motor; 30 etiquetas → calibración fiable (§8.5)
_MLX_PORT = 8765       # el cuerpo local (mlx_lm.server)


def _percentile(values: list[float], pct: float) -> float:
    """Percentil lineal (pct ∈ [0,1]); 0.0 si la lista está vacía."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    # round(x, 1) devuelve float (alineado con la anotación -> float);
    # round(x) sin ndigits devuelve int en Python 3, rompiendo el contrato.
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def _mlx_up() -> bool:
    """True si el cuerpo local (MLX :8765) acepta conexión ahora (probe rápido)."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", _MLX_PORT), timeout=0.6):
            return True
    except OSError:
        return False


def _ev_age(ts: str) -> str:
    """'hace 3h' a partir de un ts ISO (o '' si no parsea) — para la lista de pendientes."""
    from datetime import datetime
    try:
        secs = (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds()
    except (ValueError, TypeError):
        return ""
    if secs < 3600:
        return f"hace {int(secs // 60)}min"
    return f"hace {int(secs // 3600)}h" if secs < 86400 else f"hace {int(secs // 86400)}d"


def _f1_feedback_map(events: list[dict]) -> dict[str, bool]:
    """call_id → útil (último feedback gana), espejo de f1_roi._feedback_map."""
    return {e["call_id"]: bool(e.get("useful"))
            for e in events if e.get("event") == "f1_feedback" and e.get("call_id")}


def _f1_roi(f1: list[dict], feedback: dict[str, bool]) -> dict:
    """Métricas ROI de las llamadas F1 disponibles (espejo de f1_roi.compute_roi)."""
    avail = [e for e in f1 if e.get("available")]
    lats = [float(e["latency_ms"]) for e in avail if "latency_ms" in e]
    labeled = [e for e in avail if e.get("call_id") in feedback]
    return {
        "calls": len(f1),
        "availability_rate": round(len(avail) / len(f1), 2) if f1 else 0.0,
        "latency_p50": _percentile(lats, 0.5),
        "latency_p90": _percentile(lats, 0.9),
        "labeled": len(labeled),
        "useful": sum(1 for e in labeled if feedback[e["call_id"]]),
        "threshold": _CALIB_THRESHOLD,
        "ready_for_calibration": len(labeled) >= _CALIB_THRESHOLD,
    }


def _f1_pending(f1: list[dict], feedback: dict[str, bool]) -> list[dict]:
    """Llamadas F1 disponibles, con call_id, sin etiquetar — más reciente primero."""
    pend = sorted((e for e in f1 if e.get("available") and e.get("call_id")
                   and e["call_id"] not in feedback),
                  # `or ""` tolera ts=null explícito (JSON null → Python None);
                  # `.get("ts", "")` devuelve None si la clave existe con valor None.
                  key=lambda c: c.get("ts") or "", reverse=True)
    return [{"call_id": e["call_id"], "tool": e["tool"], "age": _ev_age(e.get("ts") or ""),
             "backend": e.get("backend", "?"), "chars": e.get("chars", "?")}
            for e in pend[:25]]


def read_amplifier(repo: Path | None = None) -> dict:
    """Estado del amplificador local F1: cuerpo, ROI y llamadas pendientes de etiquetar.

    FIX #2: lee el log COMPLETO (no solo un tail corto). Las llamadas F1 son raras y la
    calibración necesita el historial acumulado — espeja f1_roi que lee el log completo.
    El log rota a 50 MB (~40k líneas); la lectura completa es barata y correcta.

    Espeja la lógica de ``tools/f1_roi.py``/``f1_label.py`` del motor sobre el mismo event log,
    para que la consola sea la cabina del lazo *usar→medir→cablear* sin depender del venv.

    Returns:
        Dict con cuerpo (up/down), ROI (progreso N/30) y pendientes para etiquetar con un clic.
    """
    repo = repo or DEFAULT_REPO
    path = repo / _EVENTS
    if not path.is_file():
        return {"available": False, "reason": f"no se encontró {_EVENTS}"}
    # Leer el log completo: las llamadas F1 son raras y el tail de 20k líneas las perdía.
    # FIX #6 (round 2): fail-open ≠ fail-silencioso — OSError devuelve reason explícita.
    try:
        all_lines = [ln for ln in
                     path.read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip()]
    except OSError as _os_err:
        return {"available": True, "body_up": _mlx_up(),
                "calls": 0, "availability_rate": 0.0, "latency_p50": 0, "latency_p90": 0,
                "labeled": 0, "useful": 0, "threshold": _CALIB_THRESHOLD,
                "ready_for_calibration": False, "pending": [],
                "reason": f"OSError leyendo el log: {str(_os_err)[:80]}"}
    events = parse_events(all_lines)
    feedback = _f1_feedback_map(events)
    f1 = [e for e in events if e.get("event") == "mcp_tool" and e.get("tool") in _F1_TOOLS]
    return {"available": True, "body_up": _mlx_up(),
            **_f1_roi(f1, feedback), "pending": _f1_pending(f1, feedback)}


def _read_global_claude_servers(home: Path) -> dict:
    """Lee ``home/.claude.json`` → ``{name: raw_spec}`` (mcpServers del usuario global).

    Fuente única de verdad para la config global de MCP: tanto ``_discover_mcps()``
    (que construye el detalle con origen) como ``capabilities._local_mcp_servers()``
    (que necesita el spec completo con args/url) delegan aquí. Un solo punto de cambio
    si la ruta o el formato del archivo cambian.

    Args:
        home: HOME del usuario (p.ej. ``Path.home()``).

    Returns:
        Dict ``{name: spec}`` con los servers globales; ``{}`` si el archivo no existe
        o no tiene la clave ``mcpServers``.
    """
    path = home / ".claude.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        servers = data.get("mcpServers", {})
        return servers if isinstance(servers, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _mcp_from_global(home: Path) -> list[dict]:
    """MCP globales desde ``~/.claude.json`` con origin='global'. Fuente 1 de _discover_mcps."""
    return [
        {
            "name": name,
            "origin": "global",
            "command": (cfg.get("command") or cfg.get("url") or "")[:80],
        }
        for name, cfg in _read_global_claude_servers(home).items()
        if isinstance(name, str) and name
    ]


def _extract_mcp_servers(d: dict) -> dict:
    """Extrae ``{name: spec}`` de un dict de archivo .mcp.json.

    Soporta dos formatos:
    - Estándar: ``{"mcpServers": {name: spec}}``.
    - Top-level: ``{name: spec}`` sin wrapper (plugins como firebase/serena).

    Args:
        d: Dict ya parseado del archivo .mcp.json.

    Returns:
        Dict ``{name: spec}``; vacío si no hay servers reconocibles.
    """
    servers = d.get("mcpServers")
    if isinstance(servers, dict):
        return servers
    # Formato top-level sin wrapper: cada clave que mapea a un dict con command/url.
    return {
        k: v for k, v in d.items()
        if isinstance(k, str) and k and isinstance(v, dict)
           and ("command" in v or "url" in v)
    }


def _mcp_from_file(path: Path, origin: str) -> list[dict]:
    """Lee un .mcp.json y devuelve [{name, origin, command, url, remote}] por server. Fail-soft.

    Delega en ``_extract_mcp_servers`` para resolver el formato (estándar o top-level).

    Mantiene ``command`` y ``url`` como campos separados: colapsarlos (url→command) hacía
    que capabilities._health_mcp evaluara servers HTTP remotos (type=http, sin command)
    como stdio no-vacío → los clasificaba como mcp-stdio en vez de mcp-remote (bug figma).

    Args:
        path: Ruta al archivo .mcp.json.
        origin: Etiqueta de origen (p.ej. 'global', 'plugin:figma', 'repo').

    Returns:
        Lista de dicts ``{name, origin, command, url, remote}``; [] si falta o es inválido.
        ``remote`` es True cuando ``type`` es http/sse o hay ``url`` sin ``command``.
    """
    if not path.is_file():
        return []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return [
            {
                "name": name,
                "origin": origin,
                "command": (cfg.get("command") or "")[:80],
                "url": (cfg.get("url") or "")[:80],
                "remote": (
                    cfg.get("type", "") in ("http", "sse")
                    or (bool(cfg.get("url")) and not cfg.get("command"))
                ),
            }
            for name, cfg in _extract_mcp_servers(d).items()
            if isinstance(name, str) and name
        ]
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def _mcp_from_plugin_cache(claude_dir: Path) -> list[dict]:
    """MCPs desde el cache de plugins instalados (source 2).

    Lee ``installed_plugins.json`` para obtener la ruta de instalación activa de cada plugin
    y carga el ``.mcp.json`` de esa ruta. Un plugin puede aportar uno o más MCP servers.

    Args:
        claude_dir: ``~/.claude`` del usuario.

    Returns:
        Lista de ``{name, origin, command}`` aportados por plugins instalados.
    """
    plugins_json = claude_dir / "plugins" / "installed_plugins.json"
    if not plugins_json.is_file():
        return []
    try:
        installed = json.loads(plugins_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    entries: list[dict] = []
    for plugin_name, versions in (installed.get("plugins") or {}).items():
        short_name = plugin_name.split("@")[0]
        for version_info in (versions or []):
            install_path = (version_info or {}).get("installPath")
            if install_path:
                entries.extend(
                    _mcp_from_file(Path(install_path) / ".mcp.json",
                                   f"plugin:{short_name}"))
    return entries


def _mcp_from_local_plugins(claude_dir: Path) -> list[dict]:
    """MCPs desde plugins locales en desarrollo (source 3).

    Escanea ``~/.claude/local-plugins/*/.../.mcp.json`` (profundidad 2).

    Args:
        claude_dir: ``~/.claude`` del usuario.

    Returns:
        Lista de ``{name, origin, command}`` aportados por plugins locales.
    """
    local_plugins = claude_dir / "local-plugins"
    if not local_plugins.is_dir():
        return []
    entries: list[dict] = []
    try:
        for plugin_dir in sorted(local_plugins.iterdir()):
            if not plugin_dir.is_dir():
                continue
            for inner_dir in sorted(plugin_dir.iterdir()):
                if inner_dir.is_dir():
                    entries.extend(
                        _mcp_from_file(inner_dir / ".mcp.json",
                                       f"local-plugin:{plugin_dir.name}"))
    except OSError:
        pass
    return entries


_TELEMETRY_CACHE: dict[tuple[Path, float], list[dict]] = {}
"""Cache por (path, mtime) para _mcp_from_telemetry — evita releer el log completo (~50 MB)
en cada GET /config. Entrada se invalida automáticamente cuando el log cambia (mtime nuevo).
El dict se limpia antes de insertar una nueva entrada (efectivamente un cache de 1 slot)."""


def _parse_telemetry_servers(path: Path) -> set[str]:
    """Extrae nombres de MCP servers de eventos ``mcp_call`` del log. Fail-soft → set vacío.

    Helper de ``_mcp_from_telemetry`` — separado para mantener CC de ambas funciones ≤10.
    """
    servers: set[str] = set()
    try:
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(ln)
                if e.get("event") == "mcp_call":
                    srv = e.get("server", "")
                    if isinstance(srv, str) and srv and srv != "?":
                        servers.add(srv)
            except (json.JSONDecodeError, ValueError):
                pass
    except OSError:
        pass
    return servers


def _mcp_from_telemetry(repo: Path) -> list[dict]:
    """MCPs observados en telemetría (eventos mcp_call) — origin='runtime'.

    Fuente 4 de ``_discover_mcps``: lee el log completo para capturar el historial acumulado
    de servers remotos (connectors claude.ai) que NO están en disco. Normaliza los nombres de
    connectors claude.ai (claude_ai_X_Y → X Y) para que coincidan con lo que /cap/mcp muestra
    vía ``_add_remote_mcp``. FIX #1 (bugs clase-A round 2): cierra la brecha /config vs /cap/mcp.

    Resultado cacheado por (path, mtime): si el log no ha cambiado desde la última lectura se
    devuelve el resultado guardado sin tocar disco (PERF fix #4, 7º gate adversarial 2026-06-29).

    Args:
        repo: Raíz del repo ARIS4U (donde vive logs/v16.1-events.jsonl).

    Returns:
        Lista de ``{name, origin, command}`` por server observado; [] si el log no existe
        o no se puede leer (fail-soft).
    """
    path = repo / _EVENTS
    if not path.is_file():
        return []
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cache_key = (path, mtime)
    if cache_key in _TELEMETRY_CACHE:
        return _TELEMETRY_CACHE[cache_key]
    _TELEMETRY_CACHE.clear()  # desaloja entradas stale; es efectivamente un cache de 1 slot
    out = [
        {"name": s.replace("claude_ai_", "").replace("_", " "),
         "origin": "runtime", "command": ""}
        for s in sorted(_parse_telemetry_servers(path))
    ]
    _TELEMETRY_CACHE[cache_key] = out
    return out


def _discover_mcps(home: Path | None = None, repo: Path | None = None) -> dict:
    """Descubre los MCP efectivos desde TODAS las fuentes locales de Claude Code.

    Claude Code agrega MCPs desde varias fuentes. La fuente primaria NO es
    ``~/.claude/settings.json`` (que no contiene ``mcpServers``), sino:

    1. ``~/.claude.json`` (en HOME, NO dentro de ``.claude/``) → clave ``mcpServers``.
    2. Plugin cache → ``.mcp.json`` de cada plugin activo (figma, shadcn, etc.).
    3. Local plugins → ``.mcp.json`` en plugins en desarrollo.
    4. Telemetría → eventos ``mcp_call`` en ``logs/v16.1-events.jsonl`` (origin='runtime').
       Captura los connectors remotos de claude.ai que NO están en disco.
    5. Proyecto → ``.mcp.json`` en la raíz del repo.

    La fuente 1 se lee vía ``_read_global_claude_servers`` (fuente única compartida
    con ``capabilities._local_mcp_servers``), lo que garantiza que ``/config`` y
    ``/cap/mcp`` listen el mismo conjunto — los remotos via fuente 4 usan la misma
    normalización de nombres que ``_add_remote_mcp`` en capabilities.py.

    Args:
        home: HOME del usuario (default ``Path.home()``).
        repo: Raíz del repo del proyecto actual (para ``.mcp.json`` del proyecto y telemetría).

    Returns:
        Dict con ``mcp_global`` (nombres no-repo), ``mcp_repo`` (del proyecto),
        ``mcp_duplicated`` (en ambos) y ``mcp_by_source`` (detalle con origen por server).
    """
    home = home or Path.home()
    claude_dir = home / ".claude"
    seen: dict[str, dict] = {}  # name → primer entry (de-dup por orden de prioridad)

    sources = (
        _mcp_from_global(home),                                       # Fuente 1: ~/.claude.json
        _mcp_from_plugin_cache(claude_dir),                           # Fuente 2: plugin cache
        _mcp_from_local_plugins(claude_dir),                          # Fuente 3: local plugins
        _mcp_from_telemetry(repo) if repo is not None else [],        # Fuente 4: telemetría (runtime)
    )
    for entry in (e for src in sources for e in src):
        if entry["name"] not in seen:
            seen[entry["name"]] = entry

    repo_entries = _mcp_from_file(repo / ".mcp.json", "repo") if repo is not None else []
    global_names = sorted(seen)
    repo_names = sorted({e["name"] for e in repo_entries})
    return {
        "mcp_global": global_names,
        "mcp_repo": repo_names,
        "mcp_duplicated": sorted(set(global_names) & set(repo_names)),
        "mcp_by_source": list(seen.values()) + [e for e in repo_entries if e["name"] not in seen],
    }


def read_routing(repo: Path | None = None, *, days: int = 7) -> dict:
    """Observatorio de routing/costo (V18 Fase C): disciplina de model= en los Agent().

    Espeja ``tools/cost_report.compute_report`` sobre el mismo event log: qué fracción de
    los subagentes especificó ``model=`` explícito (vs heredó el hilo — el error #1 de V18),
    distribución por modelo/intención y costo relativo estimado. Read-only, fail-open.

    Returns:
        Dict de compute_report + {available, window_days}, o {available:False} si falta.
    """
    from datetime import datetime, timedelta

    repo = repo or DEFAULT_REPO
    log_path = repo / _EVENTS
    try:
        import sys as _sys
        if str(repo) not in _sys.path:
            _sys.path.insert(0, str(repo))
        from tools.cost_report import compute_report
        from tools.model_router import session_model

        since = datetime.now(UTC) - timedelta(days=days)
        r = compute_report(log_path, since=since, session_model=session_model())
        r["available"] = True
        r["window_days"] = days
        return r
    except Exception:
        return {"available": False, "reason": "cost_report no disponible o log ausente"}


def read_config(repo: Path | None = None) -> dict:
    """Config efectiva de ARIS4U: modelo por defecto, env/flags, MCP cableados, y dónde vive cada cosa.

    Reutiliza ``tools/aris_config.collect()`` para modelo/env/settings_path (fuente de verdad
    de ARIS4U), pero reemplaza el descubrimiento de MCP por ``_discover_mcps()``: la versión
    original solo leía ``settings.json`` (que tiene 0 ``mcpServers``); la nueva lee todas las
    fuentes reales de Claude Code: ``~/.claude.json`` (global), plugin cache, local
    plugins y el ``.mcp.json`` del repo. Read-only. Fail-soft a available=False.

    Returns:
        ``{available, model_default, env{}, mcp_global[], mcp_repo[], mcp_duplicated[],
        mcp_by_source[], settings_path}`` — o ``{available: False, reason}`` si no se puede leer.
    """
    import importlib.util

    repo = repo or DEFAULT_REPO
    cfg_path = repo / "tools" / "aris_config.py"
    if not cfg_path.is_file():
        return {"available": False, "reason": "no se encontró tools/aris_config.py"}
    try:
        spec = importlib.util.spec_from_file_location("_aris_config_console", cfg_path)
        if spec is None or spec.loader is None:
            return {"available": False, "reason": "no se pudo cargar tools/aris_config.py"}
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        data = dict(mod.collect())
        # Reemplaza el descubrimiento de MCP de aris_config (que lee settings.json vacío)
        # con el scanner multi-fuente que cubre las fuentes reales de Claude Code.
        data.update(_discover_mcps(repo=repo))
        data["available"] = True
        return data
    except Exception as e:  # fail-soft: la consola nunca 500ea por el lector de config
        return {"available": False, "reason": f"error leyendo config: {e}"}


def append_label(repo: Path | None, call_id: str, useful: bool, note: str = "") -> dict:
    """Anexa un evento f1_feedback al event log (misma forma que tools/f1_feedback.record_feedback).

    Es la escritura de la cabina: etiquetar una llamada del amplificador como útil/no. Devuelve
    el evento escrito, o ``{ok: False}`` si falla (fail-soft).
    """
    from datetime import datetime
    repo = repo or DEFAULT_REPO
    if not call_id:
        return {"ok": False, "reason": "call_id vacío"}
    event = {"ts": datetime.now(UTC).isoformat(), "hook": "f1_feedback",
             "event": "f1_feedback", "call_id": call_id, "useful": bool(useful), "note": note}
    try:
        with (repo / _EVENTS).open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        return {"ok": True, "event": event}
    except OSError as e:
        return {"ok": False, "reason": str(e)[:120]}
