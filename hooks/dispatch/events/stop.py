"""Handler Stop — portado de hooks/post_agent_verify.sh (sin heredoc shell).

Verifica los self-reports de los subagentes al fin de cada main turn: escanea el
validation log por eventos agent_dispatched/subagent_start sin agent_verify_completed,
infiere los archivos que cada agente tocó (lab_write + diff git desde repo_heads_pre)
y corre tools/agent_output_verifier.py real (compile/pub/tests rotos). Mantiene un
ledger en /tmp y un lock mkdir atómico (macOS-native). Exit 0 siempre (no bloqueante).

P0 #3 AUDIT: el .sh NUNCA corría en macOS por `ulimit -v` (no soportado en Darwin →
la cadena `&&` cortaba). Aquí NO usamos `ulimit -v`: el handler python CORRE en macOS.
El cap de CPU (ulimit -t 30) + timeout=35 siguen acotando el verifier.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path

from dispatch.contract import ARIS4U_ROOT, passthrough


def _parse_ts(s: str) -> float:
    s = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _agent_key(ev: dict) -> str:
    return f"{ev.get('ts', '?')}::{ev.get('subagent_type', 'unknown')}"


def _git_changed_files(repo: str, pre_sha: str) -> set[str]:
    """Archivos absolutos cambiados entre pre_sha y HEAD (señal secundaria, F39)."""
    if not pre_sha or not os.path.isdir(os.path.join(repo, ".git")):
        return set()
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{pre_sha}..HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return set()
        files: set[str] = set()
        for line in out.stdout.splitlines():
            line = line.strip()
            if line:
                files.add(os.path.join(repo, line))
        return files
    except Exception:
        return set()


def _run_verifier_safe(verifier_path: str, repo: str, file_list: list) -> subprocess.CompletedProcess:
    """Corre el verifier con cap de CPU (sin `ulimit -v` → corre en macOS)."""
    import platform

    # P0 #3: en Darwin NO `ulimit -v` (no soportado → la cadena cortaba y el verifier
    # jamás corría en este Mac). Solo cap de CPU; el cap de memoria queda en Linux.
    mem_cap = "" if platform.system() == "Darwin" else "ulimit -v 524288 && "
    shell_cmd = (
        mem_cap
        + "ulimit -t 30 && "  # 30s CPU máx
        + f"python3 {verifier_path!r} {repo!r} "
        + " ".join(f"'{f}'" for f in file_list)
    )
    try:
        return subprocess.run(
            ["bash", "-c", shell_cmd],
            capture_output=True,
            text=True,
            timeout=35,  # 30s ulimit + 5s gracia
        )
    except subprocess.TimeoutExpired:
        class FakeProc:
            returncode = -1
            stdout = None
            stderr = "verifier exceeded time limit"

        return FakeProc()  # type: ignore[return-value]
    except Exception as e:
        class FakeProcErr:
            returncode = -1
            stdout = None
            stderr = f"verifier invocation error: {e!r}"

        return FakeProcErr()  # type: ignore[return-value]


def _read_ledger_offset(ledger_path: str) -> int:
    """Lee el último byte-offset persistido en el ledger (`#offset:N`).

    Recorre el ledger desde el final buscando la línea de offset (H32) para
    reanudar la lectura del log sin cargarlo entero en memoria. Fail-open: ante
    cualquier error devuelve 0 (relee el log desde el inicio).

    Args:
        ledger_path: Ruta del ledger en /tmp.

    Returns:
        El último offset registrado, o 0 si no hay/ilegible.
    """
    last_offset = 0
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, "rb") as lf:
                content = lf.read()
                for line in reversed(content.split(b"\n")):
                    if line.startswith(b"#offset:"):
                        try:
                            last_offset = int(line.split(b":")[1])
                            break
                        except (ValueError, IndexError):
                            pass
        except Exception:
            pass
    return last_offset


def _read_events_from_offset(log_file: str, last_offset: int) -> tuple[list[dict], int | None]:
    """Lee en streaming los eventos JSONL del log a partir de `last_offset`.

    Lee el log en chunks de 1 MiB (no carga logs gigantes en memoria) y parsea
    cada línea que parezca un objeto JSON. Fail-open: ante cualquier error
    devuelve `([], None)` (sin eventos, sin nuevo offset).

    Args:
        log_file: Ruta del validation log JSONL.
        last_offset: Byte-offset desde el cual reanudar la lectura.

    Returns:
        Tupla `(events, new_offset)` con los eventos parseados y el byte-offset
        final tras leer; `new_offset` es None si la lectura falló.
    """
    events: list[dict] = []
    new_offset: int | None = None
    try:
        with open(log_file, "rb") as f:
            f.seek(last_offset)
            chunk = f.read(1024 * 1024)
            while chunk:
                text = chunk.decode("utf-8", errors="ignore")
                for line in text.splitlines():
                    if line.strip().startswith("{"):
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                chunk = f.read(1024 * 1024)
            new_offset = f.tell()
    except Exception:
        events = []
        new_offset = None
    return events, new_offset


def _load_ledger_keys(ledger_path: str) -> set[str]:
    """Carga las keys de agentes ya verificados desde el ledger.

    Ignora líneas vacías y de metadatos (`#offset:`). Fail-open: ante error
    devuelve un set vacío (todos los agentes se tratan como no verificados).

    Args:
        ledger_path: Ruta del ledger en /tmp.

    Returns:
        Conjunto de keys de agentes ya procesados.
    """
    ledger: set[str] = set()
    try:
        with open(ledger_path) as f:
            ledger = {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}
    except Exception:
        pass
    return ledger


def _collect_lab_write_files(
    lab_writes: list[dict], start_ts: float
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Reúne, por repo, los archivos de `lab_write` posteriores al arranque (señal primaria).

    Args:
        lab_writes: Eventos lab_write del log, ordenados por ts.
        start_ts: Timestamp (epoch) del arranque del agente; se ignoran los
            lab_write anteriores a él.

    Returns:
        Tupla `(files_by_repo, source_by_repo)` con los archivos por repo y la
        señal `lab_write` para cada repo con archivos.
    """
    files_by_repo: dict[str, set[str]] = {}
    source_by_repo: dict[str, str] = {}
    for lw in lab_writes:
        if _parse_ts(lw.get("ts", "")) < start_ts:
            continue
        path = lw.get("path") or ""
        project = lw.get("project") or ""
        if not path or not project:
            continue
        repo = project.rstrip("/")
        files_by_repo.setdefault(repo, set()).add(path)
        source_by_repo[repo] = "lab_write"
    return files_by_repo, source_by_repo


def _merge_git_signal(
    start: dict,
    files_by_repo: dict[str, set[str]],
    source_by_repo: dict[str, str],
) -> None:
    """Fusiona el diff git (`repo_heads_pre`..HEAD) sobre los archivos ya reunidos.

    Señal secundaria (F39): para cada repo con `repo_heads_pre`, une los archivos
    del diff git a los de `lab_write`. La señal resultante queda en `union` si
    ambas fuentes aportan, `git` si solo aporta git, o la previa en otro caso.
    Muta `files_by_repo` y `source_by_repo` in-place.

    Args:
        start: Evento de arranque del agente (lee `repo_heads_pre`).
        files_by_repo: Archivos por repo a actualizar in-place.
        source_by_repo: Señal por repo a actualizar in-place.
    """
    repo_heads_pre = start.get("repo_heads_pre") or {}
    for repo, pre_sha in repo_heads_pre.items():
        repo = repo.rstrip("/")
        git_files = _git_changed_files(repo, pre_sha)
        if not git_files:
            continue
        existing = files_by_repo.get(repo, set())
        files_by_repo[repo] = existing | git_files
        source_by_repo[repo] = (
            "union"
            if existing and existing != git_files
            else "git"
            if not existing
            else source_by_repo.get(repo, "lab_write")
        )


def _collect_changed_files(
    start: dict, lab_writes: list[dict], start_ts: float
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Infiere, por repo, los archivos que tocó un agente y la señal usada.

    Combina dos fuentes: los `lab_write` posteriores al arranque del agente
    (señal primaria) y el diff git desde `repo_heads_pre` (señal secundaria,
    F39). Cuando ambas aportan, la señal queda como `union`.

    Args:
        start: Evento agent_dispatched/subagent_start del agente.
        lab_writes: Eventos lab_write del log, ordenados por ts.
        start_ts: Timestamp (epoch) del arranque del agente.

    Returns:
        Tupla `(files_by_repo, source_by_repo)`: archivos por repo y la señal
        (`lab_write`/`git`/`union`) que los originó.
    """
    files_by_repo, source_by_repo = _collect_lab_write_files(lab_writes, start_ts)
    _merge_git_signal(start, files_by_repo, source_by_repo)
    return files_by_repo, source_by_repo


def _build_no_changes_event(start: dict, ts_now: str) -> dict:
    """Arma el evento `agent_verify_no_changes` para un agente sin archivos tocados.

    Args:
        start: Evento de arranque del agente.
        ts_now: Timestamp ISO de esta corrida.

    Returns:
        El evento JSONL a emitir.
    """
    return {
        "ts": ts_now,
        "hook": "post_agent_verify",
        "event": "agent_verify_no_changes",
        "subagent_type": start.get("subagent_type", "unknown"),
        "subagent_start_ts": start.get("ts"),
        "source": "stop_hook_ledger",
    }


def _parse_verifier_result(proc: subprocess.CompletedProcess, files_list: list) -> dict:
    """Parsea la salida del verifier; ante salida no-JSON devuelve un fallback.

    El verifier emite su resultado como JSON en la última línea de stdout. Si no
    se puede parsear (salida corrupta, vacía, proc reventado), se construye un
    resultado de fallback con categoría `verifier_parse_error`.

    Args:
        proc: El proceso (real o fake) devuelto por `_run_verifier_safe`.
        files_list: Archivos enviados al verifier (para `files_total`).

    Returns:
        Dict con el resultado del verifier (real o fallback).
    """
    rc = proc.returncode
    try:
        return json.loads((proc.stdout or "").strip().splitlines()[-1])
    except Exception:
        return {
            "files_total": len(files_list),
            "verified": 0,
            "pub_ok": None,
            "pub_reason": f"verifier output unparseable (rc={rc})",
            "broken_tests": [],
            "errors": [
                {
                    "category": "verifier_parse_error",
                    "severity": "error",
                    "detail": (proc.stdout or proc.stderr or "")[:200],
                }
            ],
            "warnings": [],
        }


def _build_completed_event(
    start: dict,
    repo: str,
    files_list: list,
    verifier_result: dict,
    rc: int,
    repo_source: str,
    ts_now: str,
) -> dict:
    """Arma el evento `agent_verify_completed` para un repo verificado.

    Args:
        start: Evento de arranque del agente.
        repo: Repo verificado.
        files_list: Archivos verificados (ordenados).
        verifier_result: Resultado parseado del verifier.
        rc: Exit code del verifier.
        repo_source: Señal de archivos del repo (`lab_write`/`git`/`union`).
        ts_now: Timestamp ISO de esta corrida.

    Returns:
        El evento JSONL a emitir.
    """
    return {
        "ts": ts_now,
        "hook": "post_agent_verify",
        "event": "agent_verify_completed",
        "subagent_type": start.get("subagent_type", "unknown"),
        "subagent_start_ts": start.get("ts"),
        "repo": repo,
        "files_changed_total": len(files_list),
        "verified": verifier_result.get("verified", 0),
        "errors_total": len(verifier_result.get("errors", [])),
        "pub_ok": verifier_result.get("pub_ok"),
        "pub_reason": str(verifier_result.get("pub_reason", ""))[:300],
        "broken_tests": verifier_result.get("broken_tests", [])[:20],
        "error_categories": sorted(
            {e.get("category", "?") for e in verifier_result.get("errors", [])}
        ),
        "verifier_exit": rc,
        "source": "stop_hook_ledger",
        "file_signal": repo_source,
    }


def _verify_agent(
    start: dict,
    key: str,
    lab_writes: list[dict],
    log_file: str,
    ledger_path: str,
    verifier: str,
    ts_now: str,
) -> list[dict]:
    """Verifica un agente: infiere archivos, corre el verifier y emite eventos.

    Si el agente no tocó archivos emite `agent_verify_no_changes`; si tocó,
    corre el verifier por repo y emite un `agent_verify_completed` por repo.
    En ambos casos marca la key del agente en el ledger. Salta agentes con ts
    inválido (devuelve lista vacía sin tocar el ledger).

    Args:
        start: Evento de arranque del agente.
        key: Key del agente en el ledger.
        lab_writes: Eventos lab_write del log, ordenados por ts.
        log_file: Ruta del validation log (se le hace append de eventos).
        ledger_path: Ruta del ledger (se le hace append de la key).
        verifier: Path al agent_output_verifier.py.
        ts_now: Timestamp ISO de esta corrida.

    Returns:
        Los eventos de verificación emitidos para este agente.
    """
    start_ts = _parse_ts(start.get("ts", ""))
    if start_ts == 0.0:
        return []

    files_by_repo, source_by_repo = _collect_changed_files(start, lab_writes, start_ts)

    if not files_by_repo:
        event = _build_no_changes_event(start, ts_now)
        with open(log_file, "a") as lf:
            lf.write(json.dumps(event) + "\n")
        with open(ledger_path, "a") as lg:
            lg.write(key + "\n")
        return [event]

    emitted: list[dict] = []
    for repo, file_set in files_by_repo.items():
        files_list = sorted(file_set)
        repo_source = source_by_repo.get(repo, "lab_write")
        proc = _run_verifier_safe(verifier, repo, files_list)
        rc = proc.returncode
        verifier_result = _parse_verifier_result(proc, files_list)
        event = _build_completed_event(
            start, repo, files_list, verifier_result, rc, repo_source, ts_now
        )
        with open(log_file, "a") as lf:
            lf.write(json.dumps(event) + "\n")
        emitted.append(event)

    with open(ledger_path, "a") as lg:
        lg.write(key + "\n")
    return emitted


def _append_offset(ledger_path: str, new_offset: int | None) -> None:
    """Persiste el byte-offset final en el ledger (fail-open).

    Args:
        ledger_path: Ruta del ledger.
        new_offset: Offset a registrar; si es None no se escribe nada.
    """
    if new_offset is not None:
        try:
            with open(ledger_path, "a") as lg:
                lg.write(f"#offset:{new_offset}\n")
        except Exception:
            pass


def _emit_stderr_summary(new_verifications: list[dict]) -> None:
    """Imprime a stderr un resumen de los agentes que enviaron errores.

    Solo reporta eventos `agent_verify_completed` con `errors_total > 0`, para
    visibilidad del Claude principal.

    Args:
        new_verifications: Eventos de verificación emitidos en esta corrida.
    """
    for ev in new_verifications:
        if ev.get("event") == "agent_verify_no_changes":
            continue
        err_cnt = ev.get("errors_total", 0)
        if err_cnt == 0:
            continue
        print(
            f"⚠️  V16.3 post-agent-verify — agent "
            f"{ev['subagent_type']} shipped {err_cnt} errors in {ev['repo']}.\n"
            f"   Categories: {', '.join(ev.get('error_categories', []))}\n"
            f"   Pub OK: {ev.get('pub_ok')} ({ev.get('pub_reason', '')[:120]})\n"
            f"   Broken tests: {len(ev.get('broken_tests', []))}",
            file=sys.stderr,
        )


def _verify(log_file: str, ledger_path: str, verifier: str, ts_now: str) -> None:
    """Núcleo del .sh: streaming-read del log, correlación y verificación por repo."""
    # Streaming read con byte-offset (H32) para no cargar logs gigantes en memoria.
    last_offset = _read_ledger_offset(ledger_path)
    events, new_offset = _read_events_from_offset(log_file, last_offset)

    starts = [e for e in events if e.get("event") in ("agent_dispatched", "subagent_start")]
    if not starts:
        return

    ledger = _load_ledger_keys(ledger_path)

    lab_writes = [e for e in events if e.get("event") == "lab_write"]
    lab_writes.sort(key=lambda e: _parse_ts(e.get("ts", "")))

    new_verifications: list[dict] = []

    for start in starts:
        key = _agent_key(start)
        if key in ledger:
            continue
        new_verifications.extend(
            _verify_agent(start, key, lab_writes, log_file, ledger_path, verifier, ts_now)
        )

    _append_offset(ledger_path, new_offset)

    # Resumen a stderr para visibilidad del Claude principal.
    _emit_stderr_summary(new_verifications)


def _conductor_close(session_id: str) -> str:
    """Cierre del turno (Fase 4): cierra hints ignorados + verify-gate SUAVE, y resetea señales.

    Side-effect SIEMPRE: los hints pendientes sin adoptar del turno → ``capability_ignored``
    (cierre primario por-turno; ``register_hints`` es solo respaldo) y se RESETEAN las señales
    de verificación del turno (verify_gate). El recordatorio del verify-gate va GATEADO por
    ``ARIS4U_CONDUCTOR_ENFORCE`` (OFF por defecto) → con el flag apagado devuelve ``""`` y la
    sesión normal queda intacta. El recordatorio NUNCA bloquea (lo emite el caller como
    additionalContext, no como ``decision:block``). Fail-open total.

    Args:
        session_id: Sesión cuyo turno cierra.

    Returns:
        El texto del recordatorio SUAVE a inyectar (vacío si OFF / no aplica).
    """
    try:
        if str(ARIS4U_ROOT) not in sys.path:
            sys.path.insert(0, str(ARIS4U_ROOT))
        from tools import verify_gate
        from tools.capability_adoption import flush_ignored, peek_session
        from tools.conductor_enforce import maybe_reminder

        intent, adopted = peek_session(session_id)
        reminder = maybe_reminder(intent, adopted, session_id)  # "" salvo flag ON
        flush_ignored(session_id)  # cierre de telemetría (siempre)
        verify_gate.reset_session(session_id)  # señales por-turno: limpiar tras evaluar
        return reminder
    except Exception:
        return ""


def handle(event_name: str, inp: dict) -> None:
    # Fase 4 — cierre del turno: telemetría de adopción (siempre) + verify-gate SUAVE (flag).
    # Va PRIMERO, antes de cualquier early-return del verificador (que puede no existir en
    # una instalación de tercero), para que el lazo de adopción se cierre igual.
    reminder = _conductor_close(os.environ.get("ARIS4U_SESSION_ID", ""))
    if reminder:
        # SUAVE, NUNCA bloqueante: additionalContext en Stop (no ``decision:block``, que
        # forzaría a continuar) + systemMessage para visibilidad. Exit 0 → la sesión cierra
        # normal; el recordatorio queda como contexto para el siguiente turno.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "Stop",
                        "additionalContext": reminder,
                    },
                    "systemMessage": reminder,
                }
            )
        )
        sys.exit(0)

    log_file = os.environ.get(
        "ARIS4U_LOG_FILE", str(Path.home() / "projects" / "aris4u" / "logs" / "v16.1-events.jsonl")
    )
    ledger = "/tmp/aris4u_agent_verify_ledger.txt"
    verifier = str(ARIS4U_ROOT / "tools" / "agent_output_verifier.py")

    if not os.path.isfile(log_file):
        passthrough()
    if not (os.path.isfile(verifier) and os.access(verifier, os.X_OK)):
        passthrough()

    try:
        Path(ledger).touch()
    except Exception:
        passthrough()

    # Lock atómico macOS-native vía mkdir. Si está tomado, salir silencioso (no bloquear).
    lock_dir = "/tmp/aris4u_verifier.lock.d"
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        passthrough()
    except Exception:
        passthrough()

    try:
        ts_now = datetime.now(UTC).isoformat()
        _verify(log_file, ledger, verifier, ts_now)
    finally:
        try:
            os.rmdir(lock_dir)
        except Exception:
            pass

    passthrough()
