"""Handler SessionEnd — portado de hooks/session_end.sh (sin heredoc shell).

Captura UN digest por cierre de sesión a data/sessions.db (memoria por-cliente):
git activity + decisiones + guards + token summary → save_digest (tag client_id desde
ARIS4U_CLIENT/cwd, idéntico al .sh) + espejo a claude-mem.db + F7.APRENDIZAJE. Aplica
H33 (warn si el árbol git está sucio, no-fatal) y corre el analizador throttled (300s).
Lanza en background los dos fire-and-forget del .sh (ws3 vectors + async vacuum).

CRÍTICO: la escritura de digests y la detección de client_id se preservan EXACTAS.
Side-effect verificado en tests/dispatch (digest escrito a una DB temporal).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, UTC
from pathlib import Path


def _log_event(event: dict) -> None:
    """Append a un JSONL de telemetría si está habilitado (no-op si no)."""
    log_file = os.environ.get("ARIS4U_LOG_FILE")
    if not log_file:
        return
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass


def _enrich_summary_local(
    summary: str,
    commit_count: int,
    lines_added: int,
    decisions_text: str,
    guards_text: str,
) -> str:
    """Añade una frase-narrativa (modelo LOCAL, PHI-safe) al summary determinista.

    Fase B cost-offload: tarea periférica, atómica y no-cognitiva en un modelo
    local rápido (no Claude). El summary factual se PRESERVA; la narrativa solo se
    añade. Fail-open total: si el router no responde, vuelve el summary intacto.
    Desactivable con ARIS4U_DIGEST_NARRATIVE=0.

    Args:
        summary: Summary factual determinista ya construido.
        commit_count: Commits de la sesión.
        lines_added: Líneas añadidas.
        decisions_text: Decisiones recientes (texto).
        guards_text: Guards recientes (texto).

    Returns:
        El summary, con una frase-narrativa local añadida si estuvo disponible.
    """
    if os.environ.get("ARIS4U_DIGEST_NARRATIVE", "1") == "0":
        return summary
    try:
        from engine.v16 import model_router

        facts = (
            f"Commits: {commit_count} (+{lines_added} líneas). "
            f"Decisiones: {decisions_text or 'ninguna'}. "
            f"Guards: {guards_text or 'ninguno'}."
        )
        prompt = (
            "Describe en UNA sola frase en español (máx 25 palabras) qué se logró "
            "en esta sesión de desarrollo. Empieza DIRECTO con un verbo en pasado "
            "(ej. 'Construido…', 'Cerrados…'); NO uses la palabra 'Resumen' ni dos "
            f"puntos introductorios ni comillas.\nHechos:\n{facts}"
        )
        res = model_router.route_local("digest", prompt, timeout=25)
        if res.ok and res.text:
            # risk #5: el texto puede traer razonamiento crudo — primera línea no
            # vacía, sin viñetas, capada.
            line = next((ln.strip(" -*\t") for ln in res.text.splitlines() if ln.strip()), "")
            line = line[:200].strip()
            if line:
                return f"{summary} {line}"
    except Exception:
        pass
    return summary


def _check_git_dirty(repo_cwd: Path, session_id: str) -> None:
    """H33: warn (no-fatal) si el árbol git tiene cambios sin commitear."""
    try:
        toplevel = subprocess.run(
            ["git", "-C", str(repo_cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return
    if not toplevel:
        return  # no es repo git
    try:
        porcelain = subprocess.run(
            ["git", "-C", toplevel, "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return
    dirty_files = len([ln for ln in porcelain.splitlines() if ln.strip()])
    if dirty_files <= 0:
        return

    print(
        f"[H33-WARN] Session marked as complete but git tree has "
        f"{dirty_files} uncommitted file(s).",
        file=sys.stderr,
    )
    try:
        short = subprocess.run(
            ["git", "-C", toplevel, "status", "--short"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        for ln in short.splitlines()[:10]:
            print(f"  {ln}", file=sys.stderr)
    except Exception:
        pass

    if os.environ.get("ARIS4U_VALIDATION_LOG"):
        _log_event(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "session_end_dirty_check",
                "dirty_files": dirty_files,
                "project": toplevel,
                "session_id": session_id,
                "status": "blocked_from_complete",
            }
        )


def _collect_git_activity(aris4u_root: Path) -> tuple[str, int, int]:
    """Recoge la actividad git de las últimas 8h (oneline + numstat).

    Fail-open: cualquier error devuelve los valores neutros (sin commits).

    Args:
        aris4u_root: Raíz del repo aris4u (cwd de los comandos git).

    Returns:
        Tupla (commits_raw, lines_added, commit_count).
    """
    try:
        commits_raw = subprocess.run(
            ["git", "log", "--since=8 hours ago", "--oneline"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(aris4u_root),
        ).stdout.strip()
        lines_raw = subprocess.run(
            ["git", "log", "--since=8 hours ago", "--pretty=format:", "--numstat"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(aris4u_root),
        ).stdout
        lines_added = sum(
            int(ln.split()[0])
            for ln in lines_raw.strip().split("\n")
            if ln.strip() and ln.split()[0].isdigit()
        )
        commit_count = len([ln for ln in commits_raw.split("\n") if ln.strip()])
    except Exception:
        commits_raw, lines_added, commit_count = "", 0, 0
    return commits_raw, lines_added, commit_count


def _token_summary_text() -> str:
    """Construye la línea de resumen de tokens (vacía si TokenIntelligence falla)."""
    try:
        from engine.v16.token_utils import TokenIntelligence

        ti = TokenIntelligence()
        ts = ti.session_summary()
        return (
            f"Tokens: ~{ts['total_estimated'] // 1000}k/{ts['budget_max'] // 1000}k "
            f"({ts['budget_pct']}%), {ts['queries_logged']} queries"
        )
    except Exception:
        return ""


def _build_summary(
    commit_count: int,
    lines_added: int,
    stats: dict,
    critical_guards: list,
    token_summary_text: str,
) -> str:
    """Ensambla el summary factual determinista (mismo orden y formato que el .sh).

    Args:
        commit_count: Commits de la sesión.
        lines_added: Líneas añadidas.
        stats: Dict con 'decisions' y 'guards'.
        critical_guards: Guards de severity 'critical'.
        token_summary_text: Línea de tokens ya formateada ('' si no disponible).

    Returns:
        El summary factual terminado en punto.
    """
    parts = []
    if commit_count:
        parts.append(f"{commit_count} commits, +{lines_added} lines")
    parts.append(f"DB: {stats['decisions']} decisions, {stats['guards']} guards")
    if critical_guards:
        parts.append(f"Critical guards: {len(critical_guards)}")
    if token_summary_text:
        parts.append(token_summary_text)
    return ". ".join(parts) + "."


def _collect_recent_decisions_and_guards() -> tuple[str, str]:
    """Lee decisiones/guards recientes (<8h) de sessions.db y los formatea.

    Fail-open: cualquier error de I/O o esquema devuelve ('', '').

    Returns:
        Tupla (decisions_text, guards_text), cada una posiblemente vacía.
    """
    try:
        import sqlite3

        from engine.v16.config import SESSIONS_DB

        db = sqlite3.connect(str(SESSIONS_DB))
        db.row_factory = sqlite3.Row
        # D2: exclude audit-sourced decisions from the narrative observation so
        # aris-client-audit findings don't poison the narrative memory recalled
        # by future sessions.  trust_source column added by _migrate_trust_source
        # (migration 5); the IS NULL guard preserves rows created before that
        # migration (they carry no trust_source yet and are treated as 'user').
        recent_decs = db.execute(
            "SELECT decision, domain FROM decisions "
            "WHERE created_at > datetime('now', '-8 hours') "
            "AND (trust_source IS NULL OR trust_source != 'audit') "
            "ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        recent_guards = db.execute(
            "SELECT pattern, severity FROM guards WHERE created_at > datetime('now', '-8 hours') "
            "ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        db.close()
        decisions_text = (
            "; ".join(f"[{d['domain']}] {d['decision'][:80]}" for d in recent_decs)
            if recent_decs
            else ""
        )
        guards_text = (
            "; ".join(f"[{g['severity']}] {g['pattern'][:80]}" for g in recent_guards)
            if recent_guards
            else ""
        )
    except Exception:
        decisions_text, guards_text = "", ""
    return decisions_text, guards_text


def _warn_client_id_null(session_id: str, location: str) -> None:
    """Fail-loud (no fail-ruidoso): loguea cuando client_id se escribiría NULL inesperadamente.

    El fallo es AUDITABLE pero no rompe la sesión. Dos canales:
    - stderr: visible en el terminal del usuario y en los logs de hooks.
    - JSONL (v16.1-events.jsonl): queryable por métricas/alertas futuras.

    Solo debe llamarse cuando el contexto ERA de cliente pero detect_client() devolvió None
    (pérdida de propagación). NO se llama para sesiones genuinamente sin cliente (aris4u, etc.)
    — ahí NULL es correcto y no debe alarmar. El caller decide si el NULL es inesperado.

    Args:
        session_id: ID de la sesión actual (para cruzar en el JSONL).
        location: Identificador de la función/path que detectó el NULL
                  (p.ej. "observations_local" o "save_digest").
    """
    msg = (
        f"⚠️ ARIS4U client_id=NULL [{location}] session={session_id} — "
        "detect_client() no resolvió ningún cliente en esta sesión. "
        "Si esperabas un cliente activo, verifica: (1) ARIS4U_CLIENT env, "
        "(2) cwd bajo 03-clients/<cliente>/, (3) marcador .aris-client, "
        "(4) bridge /tmp/aris4u_active_client.*.json fresco (<1h). "
        "La observación/digest se escribe con client_id=NULL (fail-open preservado)."
    )
    print(msg, file=sys.stderr)
    _log_event(
        {
            "ts": datetime.now(UTC).isoformat(),
            "event": "client_id_null_writepath",
            "session_id": session_id,
            "location": location,
            "cwd": os.getcwd(),
        }
    )


def _mirror_to_claude_mem(
    session_id: str,
    summary: str,
    decisions_text: str,
    guards_text: str,
    commit_count: int,
    lines_added: int,
) -> None:
    """V16 Phase 1: espeja el resumen de sesión a claude-mem.db (no-op si no existe).

    V18 Fase E: escribe al texto PROPIO (sessions.db/observations_local), desacoplado de
    claude-mem.db (3er-party muerta). DEDUP por content_hash — la causa raíz de la
    duplicación 7.7x era que claude-mem no tenía UNIQUE; aquí solo inserta si el hash no
    existe. Fail-open: error → stderr + ignora.
    """
    try:
        import hashlib as _hl

        from engine.v16 import session_manager as _sm

        _content = f"{summary}. Decisions: {decisions_text}. Guards: {guards_text}"
        _hash = _hl.sha256(_content.encode()).hexdigest()[:16]
        # Fix: use detect_client() instead of direct env read so the full
        # detection chain runs (env → cwd → session bridge). Reading only
        # ARIS4U_CLIENT missed clients set via .aris-client markers or the
        # session bridge (written by session_start but not always exported as
        # an env var by the time session_end fires).
        _client = _sm.detect_client()
        # Fail-loud (ítem D): si detect_client() devolvió None, el client_id se
        # escribirá NULL. Esto es correcto cuando la sesión genuinamente no tiene
        # cliente (ej: trabajo en aris4u propio). Pero si se perdió la propagación
        # (env no seteado, bridge stale, sin .aris-client), el NULL es silencioso e
        # indetectable. Loguear SIEMPRE que el cliente no se resolvió, para que sea
        # auditable. La escritura continúa (fail-open preservado).
        if _client is None:
            _warn_client_id_null(session_id, "observations_local")
        _sm.init_db()
        _db = _sm._connect()
        _db.execute(
            """
            INSERT INTO observations_local
            (id, project, type, content, content_hash, created_at, client_id)
            SELECT ?, ?, ?, ?, ?, datetime('now'), ?
            WHERE NOT EXISTS (SELECT 1 FROM observations_local WHERE content_hash = ?)
            """,
            (
                f"local-{session_id}-{_hash}",
                _client or "aris4u",
                "change",
                _content[:2000],
                _hash,
                _client,
                _hash,
            ),
        )
        _db.commit()
        _db.close()
    except Exception as _e:
        print(f"V18 observations_local capture failed: {_e}", file=sys.stderr)


def _save_ground_truth_sample(aris4u_root: Path, session_id: str) -> None:
    """Captura una muestra de ground truth para calibrar F7 (intent classifier).

    Guarda en data/f7_ground_truth.jsonl: {session_id, predicted_intent, suggestions,
    client_id, ts}. El campo `actual_type` queda vacío para llenado manual posterior.
    Con 50+ muestras revisadas, F7 puede entrenarse con datos reales en vez de heurísticas.
    Fail-open total: no rompe SessionEnd si algo falla.
    """
    try:
        pending_path = Path("/tmp/aris4u_hint_pending.json")
        if not pending_path.exists():
            return
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
        rec = pending.get("sessions", {}).get(session_id)
        if not rec:
            return
        hints = rec.get("hints", [])
        if not hints:
            return
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "predicted_intent": rec.get("intent", ""),
            "suggestions": [h.get("name", "") for h in hints],
            "adopted": [h.get("name", "") for h in hints if h.get("adopted")],
            "client_id": os.environ.get("ARIS4U_CLIENT", ""),
            "actual_type": "",  # rellenar manualmente para ground truth
        }
        # INERT: f7_ground_truth.jsonl se escribe pero nunca se lee (F7 ciego). Fable-Gate 2026-07-05.
        gt_path = aris4u_root / "data" / "f7_ground_truth.jsonl"
        with gt_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _run_learning() -> None:
    """V16: dispara F7.APRENDIZAJE al cierre de sesión (fail-open a stderr)."""
    try:
        from engine.v16.v16_orchestrator import get_orchestrator

        orch = get_orchestrator()
        learning_result = orch.end_session()
        if learning_result.get("patterns_learned", 0) > 0:
            print(
                f"F7.APRENDIZAJE: learned {learning_result['patterns_learned']} patterns",
                file=sys.stderr,
            )
    except Exception as e:
        print(f"F7.APRENDIZAJE failed: {e}", file=sys.stderr)


def _write_digest(aris4u_root: Path, session_id: str, client_id: str | None = None) -> None:
    """Núcleo: construye el resumen y persiste el digest (+claude-mem +F7 +telemetría).

    Réplica 1:1 del bloque python embebido del .sh, ejecutado in-process con el venv.
    """
    if str(aris4u_root) not in sys.path:
        sys.path.insert(0, str(aris4u_root))

    from engine.v16.session_manager import (  # noqa: F401  (init_db side-effect)
        get_all_guards,
        get_stats,
        init_db,
        save_digest,
        search,
    )

    init_db()

    commits_raw, lines_added, commit_count = _collect_git_activity(aris4u_root)

    stats = get_stats()
    guards = get_all_guards()
    critical_guards = [g for g in guards if g.get("severity") == "critical"]

    summary = _build_summary(
        commit_count, lines_added, stats, critical_guards, _token_summary_text()
    )

    built = commits_raw[:500] if commits_raw else "No commits"

    decisions_text, guards_text = _collect_recent_decisions_and_guards()

    # Fase B: enriquecer el summary factual con una frase-narrativa local (fail-open).
    summary = _enrich_summary_local(summary, commit_count, lines_added, decisions_text, guards_text)

    # Fail-loud (ítem D): si client_id llega None al digest, loguear para que sea
    # detectable. client_id viene de os.environ.get("ARIS4U_CLIENT") en handle() después
    # del intento de resolución por cwd. None aquí puede ser correcto (sesión sin cliente)
    # o puede ser pérdida de propagación — en ambos casos el log lo hace auditable.
    if client_id is None:
        _warn_client_id_null(session_id, "save_digest")

    save_digest(
        digest_id=session_id,
        summary=summary,
        built=built,
        decisions=decisions_text,
        guards=guards_text,
        tags="v16",
        session_id=session_id,
        client_id=client_id,
    )

    _mirror_to_claude_mem(
        session_id, summary, decisions_text, guards_text, commit_count, lines_added
    )

    _run_learning()

    # V16.1 Monitoring: session_end event.
    if os.environ.get("ARIS4U_VALIDATION_LOG"):
        _log_event(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "session_end",
                "commits": commit_count,
                "lines_added": lines_added,
                "decisions": stats.get("decisions", 0),
                "guards": stats.get("guards", 0),
                "session_id": session_id,
            }
        )


def _read_epoch_file(path: Path) -> int:
    """Lee un epoch entero de un archivo de estado, devolviendo 0 si falta o es ilegible.

    Args:
        path: Ruta del archivo de estado (puede no existir).

    Returns:
        El epoch leído, o 0 ante ausencia/error.
    """
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip() or "0")
    except Exception:
        return 0


def _emit_throttle_skip(throttle_state: Path, delta: int, throttle_secs: int) -> None:
    """Emite el skip-event de throttle como mucho 1×/60s (mismo gating que el .sh).

    Args:
        throttle_state: Archivo base de estado (el .skip cuelga de él).
        delta: Segundos desde el último analyze.
        throttle_secs: Ventana de throttle configurada.
    """
    now_epoch = int(datetime.now(UTC).timestamp())
    skip_state = Path(str(throttle_state) + ".skip")
    last_skip = _read_epoch_file(skip_state)
    if (
        (now_epoch - last_skip) >= 60
        and os.environ.get("ARIS4U_VALIDATION_LOG")
        and os.environ.get("ARIS4U_LOG_FILE")
    ):
        _log_event(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "auto_analyze_throttled",
                "seconds_since_last": delta,
                "throttle_secs": throttle_secs,
                "source": "session_end_hook_h11",
            }
        )
        try:
            skip_state.write_text(str(now_epoch))
        except Exception:
            pass


def _execute_analyzer(log_file: Path, analyzer: Path) -> None:
    """Corre el analizador del validation log y emite el evento completed/failed.

    Réplica del bloque interno del .sh: invoca el analyzer como subproceso, y según
    su exit code escribe auto_analyze_completed (+ resumen a stderr) o auto_analyze_failed.

    Args:
        log_file: Ruta del validation log a analizar.
        analyzer: Ruta del script analyze_validation_log.py.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(analyzer), str(log_file)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        analyze_exit = proc.returncode
        analyze_output = (proc.stdout or "") + (proc.stderr or "")
    except Exception:
        analyze_exit, analyze_output = 1, ""

    if analyze_exit == 0:
        try:
            events_count = sum(1 for _ in open(log_file))
        except Exception:
            events_count = 0
        _log_event(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "auto_analyze_completed",
                "events_analyzed": int(events_count),
                "metrics_sections": 9,
                "source": "session_end_hook",
            }
        )
        print("", file=sys.stderr)
        bar = "━" * 58
        print(bar, file=sys.stderr)
        print("## V16.1 Session Analysis", file=sys.stderr)
        print(bar, file=sys.stderr)
        print(analyze_output, file=sys.stderr)
    else:
        _log_event(
            {
                "ts": datetime.now(UTC).isoformat(),
                "event": "auto_analyze_failed",
                "reason": "analyzer exited with error",
                "source": "session_end_hook",
            }
        )


def _maybe_run_analyzer(aris4u_root: Path, throttle_state: Path, now_epoch: int) -> None:
    """Cuando NO está throttled: actualiza el estado y corre el analyzer si procede.

    Gateado por ARIS4U_VALIDATION_LOG=='1' (idéntico al .sh). El estado de throttle se
    actualiza pase lo que pase para no reintentar en el próximo Stop.

    Args:
        aris4u_root: Raíz del repo (para resolver log y analyzer por defecto).
        throttle_state: Archivo de estado de throttle a actualizar.
        now_epoch: Epoch actual a persistir.
    """
    if os.environ.get("ARIS4U_VALIDATION_LOG") != "1":
        return

    log_file = Path(
        os.environ.get("ARIS4U_LOG_FILE", str(aris4u_root / "logs" / "v16.1-events.jsonl"))
    )
    analyzer = aris4u_root / "tools" / "analyze_validation_log.py"

    # Actualizar el estado de throttle pase lo que pase (no reintentar el próximo Stop).
    try:
        throttle_state.write_text(str(now_epoch))
    except Exception:
        pass

    if log_file.exists() and analyzer.exists():
        _execute_analyzer(log_file, analyzer)


def _run_analyzer_throttled(aris4u_root: Path) -> bool:
    """Analizador throttled (H11). Devuelve True si debe seguir, False si throttled+exit.

    Réplica de la lógica de estado-en-archivo del .sh (300s default).
    """
    throttle_state = Path(
        os.environ.get("ARIS4U_ANALYZE_STATE_FILE", "/tmp/aris4u_last_auto_analyze")
    )
    throttle_secs = int(os.environ.get("ARIS4U_ANALYZE_THROTTLE_SECS", "300"))
    now_epoch = int(datetime.now(UTC).timestamp())

    last_epoch = _read_epoch_file(throttle_state)
    delta = now_epoch - last_epoch

    if delta < throttle_secs:
        _emit_throttle_skip(throttle_state, delta, throttle_secs)
        return False  # throttled → no correr analyzer

    _maybe_run_analyzer(aris4u_root, throttle_state, now_epoch)
    return True


def _launch_background(aris4u_root: Path) -> None:
    """Los dos fire-and-forget del .sh: ws3 vector backfill + async vacuum."""
    venv_py = aris4u_root / ".venv312" / "bin" / "python"
    try:
        subprocess.Popen(
            [
                str(venv_py),
                str(aris4u_root / "tools" / "ws3_backfill_vectors.py"),
                "--source",
                "observations",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass
    try:
        subprocess.Popen(
            ["bash", str(aris4u_root / "hooks" / "async_vacuum.sh")],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def handle(event_name: str, inp: dict) -> None:
    from dispatch.contract import ARIS4U_ROOT

    aris4u_root = ARIS4U_ROOT
    date = datetime.now().strftime("%Y-%m-%d")
    session_id = f"{date}_{os.urandom(4).hex()}"

    # WS4 — capturar el cliente del cwd VIVO antes de "cd" al repo, para que save_digest
    # etiquete el digest por-cliente (idéntico al .sh: deriva de /projects/03-clients/<c>/).
    orig_cwd = os.environ.get("CLAUDE_PROJECT_DIR") or inp.get("cwd") or os.getcwd()
    if not os.environ.get("ARIS4U_CLIENT"):
        # Fix: use resolve_client_from_path (canonical function) instead of a
        # manual regex+split. split("-")[0] corrupted hyphenated names such as
        # "acme-wellness" → "acme", and didn't handle .aris-client marker files.
        try:
            if str(aris4u_root) not in sys.path:
                sys.path.insert(0, str(aris4u_root))
            from engine.v16.session_manager import resolve_client_from_path as _rcfp

            client = _rcfp(orig_cwd)
        except Exception:
            m = re.search(r"/projects/03-clients/([^/]+)", orig_cwd)
            client = m.group(1).split("-")[0] if m else None
        if client:
            os.environ["ARIS4U_CLIENT"] = client

    # H33 dirty-check (sobre el cwd original, antes de movernos al repo aris4u).
    _check_git_dirty(Path(orig_cwd) if orig_cwd else aris4u_root, session_id)

    # Núcleo: digest. Fail-soft pero NO silencioso: un error aquí no debe abortar el resto,
    # PERO el write-path del digest es crítico (su rotura ya pasó desapercibida: "session_end
    # descableado 7 días → ingest 0/1503"). Se loguea a stderr + evento para que la falla sea
    # DETECTABLE, no invisible (lección del audit 2026-06-24: fail-open ≠ fail-silencioso).
    # F7 ground truth: captura muestra de datos para calibrar el intent classifier.
    _save_ground_truth_sample(aris4u_root, session_id)

    try:
        _write_digest(aris4u_root, session_id, client_id=os.environ.get("ARIS4U_CLIENT") or None)
    except Exception as e:
        print(
            f"⚠️ session_end: FALLO al escribir el digest (write-path crítico): "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        _log_event(
            {
                "event": "digest_write_failed",
                "error": f"{type(e).__name__}: {str(e)[:200]}",
                "session_id": session_id,
            }
        )

    # E3 — amplification_score: registra la puntuación de esta sesión (fail-soft).
    try:
        from engine.v16.session_manager import write_amplification_score as _write_amp
        _write_amp(session_id)
    except Exception:
        pass  # fail-open: telemetría, no bloquea el cierre de sesión

    # Analizador throttled. Si está throttled, salimos sin lanzar background (= exit 0 del .sh).
    if not _run_analyzer_throttled(aris4u_root):
        from dispatch.contract import passthrough

        passthrough()

    _launch_background(aris4u_root)

    from dispatch.contract import passthrough

    passthrough()
