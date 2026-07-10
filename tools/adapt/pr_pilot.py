#!/usr/bin/env python3
"""Modo pr-only del auto-piloto de ARIS4U (Tramo 3 §7).

Flujo cuando ARIS4U_AUTOUPDATE=pr-only:
  1. Verifica que el smoke gate pasó (exit 0 obligatorio).
  2. Crea rama ``adapt/auto-<YYYYMMDD-HHMMSS>`` desde la rama actual.
  3. Actualiza ``data/adapt_state.json`` en esa rama (fija el nuevo baseline).
  4. Hace commit + push de la rama.
  5. Abre un PR con ``gh pr create`` describiendo qué detectó y el resultado del gate.
  6. Rollback limpio si cualquier paso falla: vuelve a la rama original y borra la
     feature branch (local; si el push llegó al remote, también lo borra allá).

  **NUNCA hace auto-merge. NUNCA toca main directamente.**
  Cada paso se loguea a ``logs/adapt.jsonl`` (fail-open, no-silencioso).

Uso directo::

    python tools/adapt/pr_pilot.py \\
        --deltas-json '<JSON de watch_sources.py>' \\
        --gate-pass true \\
        [--dry-run]

Opciones:
  --deltas-json JSON   Salida JSON de watch_sources.py (campo ``deltas``, etc.).
                       Si está vacío o ausente, se trata como sin deltas y se aborta.
  --gate-pass VALUE    'true' (o 1/yes/on) si smoke_test.py pasó; 'false' si no.
  --dry-run            Imprime cada paso a stderr sin ejecutar git ni gh.
                       Siempre devuelve 0. Sí escribe en el log.

Exit codes:
  0  PR abierto (o dry-run completado sin errores).
  1  Cualquier fallo (gate roto, error git/gh); repo en estado limpio tras rollback.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("ARIS4U_ROOT") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT))

# Mismo JSONL que run_daily.sh para tener todo adapt en un archivo.
_LOG_FILE = ROOT / "logs" / "adapt.jsonl"
# Script que actualiza la baseline al invocar --update.
_WATCH_SCRIPT = ROOT / "tools" / "adapt" / "watch_sources.py"


# ---------------------------------------------------------------------------
# Internal exception — signals an adapt step failure with structured context
# ---------------------------------------------------------------------------

class _AdaptError(Exception):
    """Step helper raises this to signal failure; run_pr_pilot catches it once.

    Attributes:
        event: Log event identifier (e.g. ``adapt_pr_push_failed``).
        fields: Additional JSONL fields for the log entry.
    """

    def __init__(self, event: str, **fields: object) -> None:
        super().__init__(event)
        self.event = event
        self.fields: dict[str, object] = fields


# ---------------------------------------------------------------------------
# Logging — atomic JSONL append, fail-open, never crashes the caller
# ---------------------------------------------------------------------------

def _log(event: str, dry_run: bool = False, **fields: object) -> None:
    """Append a single JSONL record to logs/adapt.jsonl.

    Multiprocess-safe via fcntl.flock. Falls back to silent no-op if the log
    directory is unwritable — instrumentation must never crash the caller.

    Args:
        event: Identifier string (e.g. ``adapt_pr_start``).
        dry_run: Whether this is a dry-run invocation.
        **fields: Additional structured fields.
    """
    record: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        "source": "adapt:pr_pilot",
        "dry_run": dry_run,
        **fields,
    }
    try:
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.write(json.dumps(record, default=str) + "\n")
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError, ValueError):
        pass  # fail-open — log is instrumentation, not business logic


# ---------------------------------------------------------------------------
# Subprocess wrapper — mockable, fail-fast, dry-run aware
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, cwd: str | None = None, capture: bool = True, dry_run: bool = False, label: str = "") -> subprocess.CompletedProcess[str]:  # noqa: E501
    """Run a command, raise CalledProcessError on non-zero exit.

    In dry-run mode the command is printed to stderr and a synthetic
    successful CompletedProcess is returned without executing anything.

    Args:
        cmd: Command and arguments list.
        cwd: Working directory; defaults to ``ROOT``.
        capture: If True, capture stdout/stderr (standard for git/gh calls).
        dry_run: If True, only print — do not execute.
        label: Short human-readable tag for dry-run output.

    Returns:
        CompletedProcess with returncode=0 (dry-run) or actual subprocess result.

    Raises:
        subprocess.CalledProcessError: Non-zero exit in real mode.
    """
    wd = cwd or str(ROOT)
    if dry_run:
        tag = f"[DRY-RUN:{label}]" if label else "[DRY-RUN]"
        print(f"{tag} {' '.join(str(c) for c in cmd)}", file=sys.stderr)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
    result = subprocess.run(cmd, cwd=wd, capture_output=capture, text=True)
    result.check_returncode()
    return result


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _current_branch(*, dry_run: bool = False) -> str:
    """Return current git branch name.

    Returns ``"main"`` in dry-run mode or if git is unavailable (fail-open).

    Args:
        dry_run: If True, return ``"main"`` without calling git.

    Returns:
        Current branch name or ``"main"`` as safe fallback.
    """
    if dry_run:
        return "main"
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout.strip() or "main"
    except Exception:
        return "main"


def _rollback(branch: str, original_branch: str, dry_run: bool, *, delete_remote: bool = False) -> None:
    """Return to the original branch and delete the feature branch.

    Fail-open: each sub-step is independently guarded so one failure does not
    prevent subsequent cleanup. Logs every sub-step result.

    Args:
        branch: Feature branch to delete (local, and remote if delete_remote).
        original_branch: Branch to check out before deleting.
        dry_run: If True, only print what would happen.
        delete_remote: Also push --delete origin <branch> if True.
    """
    _log("adapt_pr_rollback_start", dry_run=dry_run,
         branch=branch, target=original_branch, delete_remote=delete_remote)
    try:
        _run(["git", "checkout", original_branch],
             dry_run=dry_run, label="rollback:checkout")
    except subprocess.CalledProcessError as exc:
        _log("adapt_pr_rollback_checkout_failed", dry_run=dry_run,
             branch=branch, error=str(exc))
    try:
        _run(["git", "branch", "-D", branch],
             dry_run=dry_run, label="rollback:delete-local")
        _log("adapt_pr_rollback_local_deleted", dry_run=dry_run, branch=branch)
    except subprocess.CalledProcessError as exc:
        _log("adapt_pr_rollback_delete_local_failed", dry_run=dry_run,
             branch=branch, error=str(exc))
    if delete_remote:
        try:
            _run(["git", "push", "origin", "--delete", branch],
                 dry_run=dry_run, label="rollback:delete-remote")
            _log("adapt_pr_rollback_remote_deleted", dry_run=dry_run, branch=branch)
        except subprocess.CalledProcessError as exc:
            _log("adapt_pr_rollback_delete_remote_failed", dry_run=dry_run,
                 branch=branch, error=str(exc))
    _log("adapt_pr_rollback_done", dry_run=dry_run, branch=branch)


# ---------------------------------------------------------------------------
# Step helpers — each raises _AdaptError on failure (caught once in main flow)
# ---------------------------------------------------------------------------

def _step_checkout_branch(branch: str, dry_run: bool) -> None:
    """Create and switch to the feature branch.

    Args:
        branch: Branch name to create (e.g. ``adapt/auto-20260101-120000``).
        dry_run: If True, only print the git command.

    Raises:
        _AdaptError: If git checkout -b fails.
    """
    try:
        _run(["git", "checkout", "-b", branch],
             dry_run=dry_run, label="create-branch")
    except subprocess.CalledProcessError as exc:
        raise _AdaptError("adapt_pr_branch_failed",
                          branch=branch, error=str(exc)) from exc


def _step_update_baseline(branch: str, dry_run: bool) -> None:
    """Run watch_sources.py --update to persist the new baseline to disk.

    Args:
        branch: Current feature branch (for error context only).
        dry_run: If True, only print the command.

    Raises:
        _AdaptError: If watch_sources --update fails or state file is absent.
    """
    try:
        _run([sys.executable, str(_WATCH_SCRIPT), "--update"],
             dry_run=dry_run, label="update-baseline")
    except subprocess.CalledProcessError as exc:
        raise _AdaptError("adapt_pr_baseline_update_failed",
                          branch=branch, error=str(exc)) from exc
    state_path = ROOT / "data" / "adapt_state.json"
    if not dry_run and not state_path.exists():
        raise _AdaptError("adapt_pr_state_missing",
                          branch=branch, path=str(state_path),
                          error="adapt_state.json absent after watch_sources --update")


def _step_commit(commit_msg: str, branch: str, dry_run: bool) -> None:
    """Stage adapt_state.json and commit on the feature branch.

    Args:
        commit_msg: Git commit message body.
        branch: Current feature branch (for error context).
        dry_run: If True, only print git commands.

    Raises:
        _AdaptError: If git add or git commit fails.
    """
    try:
        _run(["git", "add", "data/adapt_state.json"],
             dry_run=dry_run, label="git-add")
        _run(["git", "commit", "-m", commit_msg],
             dry_run=dry_run, label="git-commit")
    except subprocess.CalledProcessError as exc:
        raise _AdaptError("adapt_pr_commit_failed",
                          branch=branch, error=str(exc)) from exc


def _step_push(branch: str, dry_run: bool) -> None:
    """Push the feature branch to origin.

    Args:
        branch: Feature branch name.
        dry_run: If True, only print the git command.

    Raises:
        _AdaptError: If git push fails.
    """
    try:
        _run(["git", "push", "--set-upstream", "origin", branch],
             dry_run=dry_run, label="git-push")
    except subprocess.CalledProcessError as exc:
        raise _AdaptError("adapt_pr_push_failed",
                          branch=branch, error=str(exc)) from exc


def _step_create_pr(pr_title: str, pr_body: str, branch: str, base: str, dry_run: bool) -> str:
    """Open a GitHub PR with gh pr create.

    Args:
        pr_title: PR title string.
        pr_body: PR body Markdown.
        branch: Head branch (feature branch).
        base: Base branch (typically ``"main"``).
        dry_run: If True, only print the gh command.

    Returns:
        The PR URL returned by gh, or ``"(dry-run)"`` in dry-run mode.

    Raises:
        _AdaptError: If gh pr create fails.
    """
    try:
        result = _run(
            ["gh", "pr", "create",
             "--title", pr_title,
             "--body", pr_body,
             "--head", branch,
             "--base", base],
            dry_run=dry_run,
            label="gh-pr-create",
        )
        return result.stdout.strip() if not dry_run else "(dry-run: no PR created)"
    except subprocess.CalledProcessError as exc:
        raise _AdaptError("adapt_pr_gh_create_failed",
                          branch=branch, error=str(exc)) from exc


# ---------------------------------------------------------------------------
# PR body builder
# ---------------------------------------------------------------------------

def _build_pr_body(deltas: dict[str, Any], gate_pass: bool, branch: str) -> str:
    """Build the Markdown body for gh pr create.

    Args:
        deltas: Parsed output dict from watch_sources.py.
        gate_pass: Whether the smoke gate exited 0.
        branch: The feature branch name.

    Returns:
        Markdown string ready for the --body flag.
    """
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    mech: list[dict[str, Any]] = deltas.get("mechanical", [])
    sem: list[dict[str, Any]] = deltas.get("semantic", [])
    all_deltas: list[dict[str, Any]] = deltas.get("deltas", [])

    lines = [
        "## ARIS4U Auto-Adapt — pr-only (Tramo 3 §7)",
        "",
        f"**Detectado:** {ts}  ",
        f"**Rama:** `{branch}`  ",
        f"**Gate (smoke):** {'PASS ✅' if gate_pass else 'FAIL ❌'}",
        "",
    ]

    if all_deltas:
        lines += ["### Cambios detectados", ""]
        for d in all_deltas:
            route_tag = d.get("route", "unknown")
            lines.append(
                f"- **{d.get('source', '?')}** "
                f"`{d.get('old', '?')}` → `{d.get('new', '?')}` _({route_tag})_"
            )
        lines.append("")

    if mech:
        lines += [
            "### Cambios mecánicos",
            f"{len(mech)} cambio(s) determinístico(s): bump de versión / modelo / settings. "
            "Revisar que el nuevo baseline sea correcto y hacer merge.",
            "",
        ]
    if sem:
        lines += [
            "### Cambios semánticos",
            f"{len(sem)} cambio(s) que requieren interpretación: el changelog de Claude cambió. "
            "Revisar las notas del release antes de hacer merge.",
            "",
        ]

    lines += [
        "---",
        "**Este PR NUNCA se auto-mergea.** Revisión humana requerida antes de merge.",
        "",
        "_Generado automáticamente por ARIS4U adapt:pr\\_pilot (Tramo 3 §7)_",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main flow — single exception handler keeps CC low
# ---------------------------------------------------------------------------

def run_pr_pilot(deltas_json: str, gate_pass: bool, dry_run: bool = False) -> int:
    """Execute the pr-only autopilot: gate → branch → commit → push → PR.

    Uses ``_AdaptError`` internally so each git/gh step can signal failure
    without nested try/except chains.  A single handler does rollback and
    logging, keeping cyclomatic complexity manageable.

    NEVER pushes to main. NEVER auto-merges.

    Args:
        deltas_json: JSON string produced by ``watch_sources.py``.
                     Empty string or invalid JSON → immediate exit 1.
        gate_pass: True if ``smoke_test.py`` exited 0.
        dry_run: Simulate all steps without running git or gh. Always returns 0.

    Returns:
        0 on success or dry-run completion; 1 on any failure.
    """
    if not deltas_json:
        _log("adapt_pr_bad_deltas_json", dry_run=dry_run,
             error="empty deltas_json — nothing to do")
        return 1
    try:
        deltas: dict[str, Any] = json.loads(deltas_json)
    except json.JSONDecodeError as exc:
        _log("adapt_pr_bad_deltas_json", dry_run=dry_run, error=str(exc))
        return 1

    if not gate_pass:
        _log("adapt_pr_gate_failed", dry_run=dry_run,
             reason="smoke gate no pasó — PR abortado para no propagar cambios rotos")
        print("[ARIS4U adapt:pr-only] smoke gate FALLÓ — no se abre PR.", file=sys.stderr)
        return 1

    original_branch = _current_branch(dry_run=dry_run)
    ts_tag = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    branch = f"adapt/auto-{ts_tag}"
    all_deltas: list[dict[str, Any]] = deltas.get("deltas", [])
    srcs = ",".join(d.get("source", "?") for d in all_deltas)
    n_mech = len(deltas.get("mechanical", []))
    n_sem = len(deltas.get("semantic", []))

    _log("adapt_pr_start", dry_run=dry_run, branch=branch,
         original_branch=original_branch, sources=srcs,
         mechanical=n_mech, semantic=n_sem, gate_pass=gate_pass)

    commit_msg = (
        f"adapt: bump baseline {ts_tag} ({srcs or 'unknown'})\n\n"
        f"Auto-detected change in: {srcs or '(none listed)'}\n"
        f"Gate (smoke): PASS\n"
        f"Mechanical: {n_mech} / Semantic: {n_sem}\n"
        f"[ARIS4U adapt:pr-only Tramo 3 §7]"
    )
    pr_title = f"[ARIS4U adapt] Bump baseline {ts_tag} — {srcs or 'unknown'}"
    pr_body = _build_pr_body(deltas, gate_pass, branch)

    branch_created = False
    pushed = False
    try:
        _step_checkout_branch(branch, dry_run)
        branch_created = True
        _step_update_baseline(branch, dry_run)
        _step_commit(commit_msg, branch, dry_run)
        _step_push(branch, dry_run)
        pushed = True
        pr_url = _step_create_pr(pr_title, pr_body, branch, original_branch, dry_run)
    except _AdaptError as exc:
        _log(exc.event, dry_run=dry_run, **exc.fields)
        print(f"[ARIS4U adapt:pr-only] ERROR en '{exc.event}'", file=sys.stderr)
        if branch_created:
            _rollback(branch, original_branch, dry_run, delete_remote=pushed)
        return 1

    _log("adapt_pr_opened", dry_run=dry_run, branch=branch,
         sources=srcs, pr_url=pr_url, mechanical=n_mech, semantic=n_sem)
    print(
        f"[ARIS4U adapt:pr-only] PR abierto: {pr_url}  "
        f"(rama {branch}, fuentes: {srcs or 'unknown'})",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for pr_pilot.

    Args:
        argv: Argument list; defaults to sys.argv[1:].

    Returns:
        Parsed namespace with deltas_json, gate_pass, dry_run attributes.
    """
    p = argparse.ArgumentParser(
        description="ARIS4U adapt pr-only pilot — abre PR sin auto-merge.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--deltas-json",
        default="",
        metavar="JSON",
        help="JSON output from watch_sources.py; empty string → abort.",
    )
    p.add_argument(
        "--gate-pass",
        default="true",
        metavar="BOOL",
        help="'true' if smoke_test.py passed (exit 0); 'false' aborts PR.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate without executing git or gh. Always exits 0.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and run the pr-only pilot.

    Args:
        argv: Argument list; defaults to sys.argv[1:].

    Returns:
        Exit code (0 = success/dry-run, 1 = failure).
    """
    args = _parse_args(argv)
    gate_pass = args.gate_pass.strip().lower() in ("true", "1", "yes", "on")
    return run_pr_pilot(
        deltas_json=args.deltas_json,
        gate_pass=gate_pass,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
