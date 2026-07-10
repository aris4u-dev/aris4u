"""Runner de build para ARIS4U cowork — modo ``--once`` (humano-en-el-loop).

Toma el intake ``pending`` más antiguo, transiciona a ``building``, crea el repo
greenfield destino, lanza el build headless con ``claude -p`` y registra la
corrida en ``build_runs``.

Diseño:
- **Un intake por corrida** (``--once``): sin daemon, sin bucle.
- **Launcher inyectable**: ``run_once(db, *, launcher=_default_launcher, base_dir=...)``
  — en producción usa subprocess real; los tests inyectan un fake sin invocar ``claude``.
- **Seguridad MVP greenfield-only**: si el directorio destino ya existe con contenido,
  aborta sin tocar nada.
- **Idempotencia de tabla**: ``ensure_build_runs_table`` sigue el patrón once-per-process
  de ``cowork_intake.ensure_intake_table``.

CLI::

    python3 tools/cowork_runner.py --once
    python3 tools/cowork_runner.py --once --db data/sessions.db --base-dir ~/projects/05-cowork
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO_ROOT / "data" / "sessions.db"
_DEFAULT_BASE_DIR = Path.home() / "projects" / "05-cowork"

# ---------------------------------------------------------------------------
# Slug derivation
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _make_slug(client_id: str, intake_id: int | str) -> str:
    """Derive a filesystem-safe slug from client_id and intake_id.

    Format: ``<client_id>-<short_id>`` where short_id is the last 8 hex chars
    of intake_id (or the raw int stringified if not hex).  Output is
    ``[a-z0-9-]+`` with no leading/trailing hyphens.

    Args:
        client_id: Client identifier (already validated ``[a-z0-9_-]+``).
        intake_id: Integer PK of the intake row.

    Returns:
        Slug string safe for use as a directory name.
    """
    base = client_id.lower().replace("_", "-")
    base = _SLUG_RE.sub("-", base).strip("-")
    short = str(intake_id)[-8:]
    return f"{base}-{short}"


# ---------------------------------------------------------------------------
# Table migration — build_runs (once-per-process, same pattern as intake)
# ---------------------------------------------------------------------------

_BUILD_RUNS_TABLE_READY: bool = False


def ensure_build_runs_table(db_path: str | Path) -> None:
    """Create ``build_runs`` table if it does not exist (idempotent, once-per-process).

    Follows the ``_INTAKE_TABLE_READY`` pattern from ``cowork_intake``:
    a module-level flag avoids redundant CREATE TABLE calls within a process.

    Args:
        db_path: Path to the SQLite file (sessions.db).
    """
    global _BUILD_RUNS_TABLE_READY
    if _BUILD_RUNS_TABLE_READY:
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS build_runs (
                run_id      INTEGER PRIMARY KEY,
                intake_id   INTEGER NOT NULL,
                client_id   TEXT    NOT NULL,
                repo_path   TEXT    NOT NULL,
                log_path    TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'running',
                started_at  TEXT    NOT NULL,
                ended_at    TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()
    _BUILD_RUNS_TABLE_READY = True


# ---------------------------------------------------------------------------
# build_runs CRUD
# ---------------------------------------------------------------------------


def create_build_run(
    db_path: str | Path,
    intake_id: int,
    client_id: str,
    repo_path: str | Path,
    log_path: str | Path,
) -> int:
    """Insert a new build_run row with status ``running`` and return its run_id.

    Args:
        db_path: Path to sessions.db.
        intake_id: FK to intake_requests.id.
        client_id: Client identifier (for quick queries without join).
        repo_path: Absolute path to the greenfield repo.
        log_path: Absolute path where build stdout/stderr is written.

    Returns:
        The new ``run_id`` (INTEGER PRIMARY KEY).

    Raises:
        sqlite3.Error: On insertion failure.
    """
    ensure_build_runs_table(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO build_runs (intake_id, client_id, repo_path, log_path, status, started_at)
            VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (intake_id, client_id, str(repo_path), str(log_path), now),
        )
        conn.commit()
        run_id: int = cur.lastrowid  # type: ignore[assignment]
    finally:
        conn.close()
    return run_id


def finish_build_run(
    db_path: str | Path,
    run_id: int,
    status: str,
) -> None:
    """Update a build_run to its final status and set ended_at.

    Args:
        db_path: Path to sessions.db.
        run_id: PK of the build_run to update.
        status: Final status — ``'done'``, ``'failed'``, or ``'needs_review'``.

    Raises:
        ValueError: If status is not one of the valid final statuses.
        sqlite3.Error: On update failure.
    """
    if status not in {"done", "failed", "needs_review"}:
        raise ValueError(f"status inválido '{status}'; válidos: done, failed, needs_review")
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "UPDATE build_runs SET status = ?, ended_at = ? WHERE run_id = ?",
            (status, now, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def repo_for_client(db_path: str | Path, client_id: str) -> str | None:
    """Return the repo_path of the most recent build_run for a client, or None.

    Read-only (mode=ro URI).  Returns None when the build_runs table does not
    exist yet, the DB is absent, or the client has no build runs at all.

    Args:
        db_path: Path to sessions.db.
        client_id: Client identifier to scope the query.

    Returns:
        Absolute repo path string (as stored in build_runs.repo_path) of the
        most recent run for this client, or None if absent.
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
            "SELECT repo_path FROM build_runs "
            "WHERE client_id = ? "
            "ORDER BY started_at DESC LIMIT 1",
            (client_id,),
        ).fetchone()
        return str(row["repo_path"]) if row else None
    except sqlite3.OperationalError:
        # build_runs table does not exist yet
        return None
    finally:
        conn.close()


def list_build_runs(
    db_path: str | Path,
    status: str | None = None,
) -> list[dict]:
    """Return build_runs rows, optionally filtered by status.

    Args:
        db_path: Path to sessions.db.
        status: If given, filter to this exact status value.  ``None`` = all rows.

    Returns:
        List of dicts with all build_runs columns, ordered by ``started_at DESC``.
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
                "SELECT run_id, intake_id, client_id, repo_path, log_path, "
                "status, started_at, ended_at "
                "FROM build_runs WHERE status = ? ORDER BY started_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT run_id, intake_id, client_id, repo_path, log_path, "
                "status, started_at, ended_at "
                "FROM build_runs ORDER BY started_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Repo initialisation
# ---------------------------------------------------------------------------


def _init_repo(repo_path: Path, brief_text: str) -> None:
    """Initialise a greenfield git repo with an initial commit.

    Creates ``repo_path``, writes ``README.md`` with the brief text, runs
    ``git init`` and commits.  Timeout = 30 s per subprocess call.

    Args:
        repo_path: Directory to create and initialise (must not exist yet, or
            be empty — caller guarantees the safety check).
        brief_text: Content to write into ``README.md``.

    Raises:
        subprocess.TimeoutExpired: If git commands take longer than 30 s.
        subprocess.CalledProcessError: If any git command fails.
        OSError: If the directory cannot be created.
    """
    repo_path.mkdir(parents=True, exist_ok=True)
    readme = repo_path / "README.md"
    readme.write_text(brief_text, encoding="utf-8")

    _git = ["git", "-C", str(repo_path)]
    subprocess.run([*_git, "init"], check=True, capture_output=True, timeout=30)
    subprocess.run([*_git, "add", "README.md"], check=True, capture_output=True, timeout=30)
    subprocess.run(
        [*_git, "commit", "-m", "chore: initial commit from cowork intake"],
        check=True, capture_output=True, timeout=30,
        env={**_git_env()},
    )


def _git_env() -> dict[str, str]:
    """Return a minimal env for git subprocess calls with required vars set."""
    import os
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", "ARIS4U Runner")
    env.setdefault("GIT_AUTHOR_EMAIL", "runner@aris4u.local")
    env.setdefault("GIT_COMMITTER_NAME", "ARIS4U Runner")
    env.setdefault("GIT_COMMITTER_EMAIL", "runner@aris4u.local")
    return env


# ---------------------------------------------------------------------------
# Default launcher (production — calls real claude)
# ---------------------------------------------------------------------------


def _default_launcher(cmd: list[str], cwd: Path, log_path: Path) -> int:
    """Launch ``cmd`` in ``cwd``, redirecting output to ``log_path``.

    This is the production launcher.  Tests ALWAYS inject a fake launcher
    instead — the real ``claude`` binary is never called in tests.

    Args:
        cmd: Command + args to execute (e.g. ``["claude", "-p", "<prompt>"]``).
        cwd: Working directory for the subprocess.
        log_path: File path where stdout and stderr are written.

    Returns:
        Process returncode (0 = success).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            stdout=fh,
            stderr=subprocess.STDOUT,
            timeout=3600,  # 1-hour hard cap for a full build
        )
    return proc.returncode


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


def _build_prompt(brief_path: Path, docs_dir: Path, repo_path: Path) -> str:
    """Construct the headless claude prompt for the build.

    The prompt is fully self-contained for ``claude -p`` (non-interactive, no
    human present, no Workflow engine).  It MUST NOT reference any interactive
    skill (``/clarify``) or Workflow (``enterprise-build``).  All phases are
    expressed as direct prose instructions that ``claude -p`` can execute
    without further input.

    Phases:
      DISCOVER  — infer domain, entities, business rules from brief + docs.
      CONTRACT  — define modules and minimal file structure before building.
      BUILD     — create all project files in ``repo_path`` and commit per module.
      VERIFY    — run tests/imports if generated; leave project in working state.

    Progress is measured by git commits, not by text output.

    Args:
        brief_path: Absolute path to the intake brief.md.
        docs_dir: Absolute path to the intake docs/ directory.
        repo_path: Absolute path to the greenfield destination repo (already
            git-initialised with one initial commit).

    Returns:
        Prompt string to pass to ``claude -p``.
    """
    return (
        "You are building a software project from a product brief in HEADLESS/"
        "non-interactive mode. There is NO human present — NEVER ask questions, "
        "NEVER use AskUserQuestion, NEVER invoke interactive skills or workflows. "
        "Infer everything from the brief and documents; document your assumptions "
        "as inline comments or in README.md.\n\n"
        f"Brief: {brief_path}\n"
        f"Supporting docs: {docs_dir}\n"
        f"Destination repo (already git-initialised): {repo_path}\n\n"
        "Follow these four phases in order:\n\n"
        "## PHASE 1 — DISCOVER\n"
        f"Read the brief at {brief_path} and every file under {docs_dir} "
        "(skip the directory itself if empty). "
        "Identify: primary domain, core entities, key business rules, "
        "integration points, and any explicit non-functional requirements. "
        "Write a brief summary of your understanding as a comment in "
        f"{repo_path}/README.md (append, do not overwrite the initial content).\n\n"
        "## PHASE 2 — CONTRACT\n"
        "Define the minimal module/file structure required to satisfy the brief. "
        "List the top-level files and directories you will create. "
        "If the brief is ambiguous on a detail, choose the most conventional "
        "interpretation for the domain and note the assumption.\n\n"
        "## PHASE 3 — BUILD\n"
        f"Create all project files inside {repo_path}. "
        "Work only inside that directory — do NOT touch any file outside it. "
        "After completing each logical module or layer, stage and commit:\n"
        "  git -C {repo_path} add -A\n"
        "  git -C {repo_path} commit -m '<module>: <one-line description>'\n"
        "Minimum deliverables: README.md (updated), project structure, "
        "core source files that implement the main entities/logic, and "
        "any configuration files (e.g. pyproject.toml, package.json) needed "
        "to run or import the code. Aim for at least 2 commits beyond the "
        "initial one.\n\n"
        "## PHASE 4 — VERIFY\n"
        "If you generated tests, attempt to run them and log the result to "
        f"{repo_path}/.cowork/verify.log. "
        "If they cannot run in this environment, note why in the log. "
        "Ensure the project is in a state that can be imported or executed "
        "(no syntax errors, no missing __init__.py, etc.).\n\n"
        "## COMMIT YOUR WORK\n"
        "Stage and commit any remaining changes before finishing:\n"
        "  git -C {repo_path} add -A\n"
        "  git -C {repo_path} commit -m 'chore: final polish and verify'\n"
        "Progress is measured exclusively by git commits — text output alone "
        "does not count as done."
    ).replace("{repo_path}", str(repo_path))


# ---------------------------------------------------------------------------
# Done criterion — mechanical commit check (B1-bis)
# ---------------------------------------------------------------------------


def _build_produced_commits(repo_path: Path, base_sha: str) -> bool:
    """Return True iff the repo contains commits beyond base_sha.

    This is the **mechanical done criterion**: a build is considered successful
    only when claude actually created at least one new commit in the repo,
    regardless of the subprocess returncode.

    Algorithm:
      1. Count total commits: ``git rev-list --count HEAD``.
      2. Count commits reachable from base_sha: ``git rev-list --count <sha>``.
      3. If (total − base_count) > 0, the build produced new commits → done.

    Falls back to ``False`` on any git error (fail-safe for done criterion:
    ambiguous cases are treated as not-done rather than false-done).

    Args:
        repo_path: Absolute path to the git repository.
        base_sha: The SHA of the last commit present BEFORE the build started
            (i.e. the initial commit created by ``_init_repo``).

    Returns:
        ``True`` if at least one new commit exists beyond base_sha.
    """
    try:
        total_result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if total_result.returncode != 0:
            _log.warning("git rev-list failed for %s: %s", repo_path, total_result.stderr.strip())
            return False
        total_commits = int(total_result.stdout.strip())

        base_result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-list", "--count", base_sha],
            capture_output=True, text=True, timeout=15,
        )
        if base_result.returncode != 0:
            _log.warning("git rev-list for base sha failed: %s", base_result.stderr.strip())
            return False
        base_commits = int(base_result.stdout.strip())

        new_commits = total_commits - base_commits
        _log.info("commit check: total=%d base=%d new=%d", total_commits, base_commits, new_commits)
        return new_commits > 0
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        _log.warning("_build_produced_commits error for %s: %s", repo_path, exc)
        return False


def _get_head_sha(repo_path: Path) -> str | None:
    """Return the current HEAD commit SHA, or None on failure.

    Args:
        repo_path: Absolute path to the git repository.

    Returns:
        40-char hex SHA string, or None if git fails.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Core: run_once
# ---------------------------------------------------------------------------

# Type alias for the launcher callable signature
Launcher = Callable[[list[str], Path, Path], int]


def run_once(
    db_path: str | Path,
    *,
    launcher: Launcher = _default_launcher,
    base_dir: str | Path | None = None,
) -> dict | None:
    """Process the oldest pending intake (one per call).

    Flow:
      1. Find oldest pending intake via ``list_intakes(db, 'pending')``.
         Returns ``None`` immediately if none found.
      2. Transition intake to ``building``.
      3. Derive slug and create greenfield repo at ``base_dir/<slug>/``.
         Aborts with ``RuntimeError`` if the directory already has content.
      4. Create ``build_runs`` row (status ``running``).
      5. Call ``launcher(cmd, cwd=repo_path, log_path=...)``.
      6. Finish build_run (``done``/``failed``) and transition intake accordingly.
      7. Return summary dict.

    Args:
        db_path: Path to sessions.db.
        launcher: Callable ``(cmd, cwd, log_path) -> returncode``.  Defaults to
            the real subprocess launcher.  Tests inject a fake to avoid invoking
            ``claude``.
        base_dir: Parent directory for greenfield repos.  Defaults to
            ``~/projects/05-cowork``.

    Returns:
        Summary dict ``{intake_id, slug, repo_path, run_id, status, log_path}``
        or ``None`` if there were no pending intakes.

    Raises:
        RuntimeError: If the destination repo already has content (safety abort).
        sqlite3.Error: On DB failures.
        subprocess.TimeoutExpired: If git init times out.
    """
    # Resolve paths
    db_path = Path(db_path)
    resolved_base = Path(base_dir) if base_dir is not None else _DEFAULT_BASE_DIR

    # Ensure table exists before any read
    ensure_build_runs_table(db_path)

    # 1. Find oldest pending intake (list returns DESC; last = oldest)
    from tools.cowork_intake import (  # noqa: E402  (local import — avoids circular at module load)
        ensure_intake_table,
        list_intakes,
        set_status,
    )
    ensure_intake_table(db_path)

    pending = list_intakes(db_path, status="pending")
    if not pending:
        _log.info("no pending intakes — nothing to do")
        return None

    # list_intakes returns DESC; oldest is the last element
    intake = pending[-1]
    intake_id: int = intake["id"]
    client_id: str = intake["client_id"]
    brief_path_rel: str = intake["brief_path"]
    docs_dir_rel: str = intake["docs_dir"]

    # Resolve relative paths stored in DB against the data root (db_path.parent),
    # then make them ABSOLUTE: the build runs headless with cwd=<client repo>, so a
    # relative brief path embedded in the prompt would not resolve from there.
    brief_path = (db_path.parent / brief_path_rel).resolve()
    docs_dir = (db_path.parent / docs_dir_rel).resolve()

    # 2. Transition to building
    set_status(db_path, intake_id, "building")
    _log.info("intake %s: transitioning to building", intake_id)

    # 3. Greenfield repo
    slug = _make_slug(client_id, intake_id)
    repo_path = resolved_base / slug

    if repo_path.exists() and any(repo_path.iterdir()):
        # Safety abort: refuse to overwrite non-empty directory
        set_status(db_path, intake_id, "failed")
        raise RuntimeError(
            f"repo destino ya existe y no está vacío: {repo_path} — abortando para no sobreescribir"
        )

    # Read brief for README (fail-open: use placeholder if not readable)
    brief_text = _read_brief(brief_path)

    try:
        _init_repo(repo_path, brief_text)
    except (subprocess.SubprocessError, OSError) as exc:
        set_status(db_path, intake_id, "failed")
        raise RuntimeError(f"git init falló para {repo_path}: {exc}") from exc

    # B1-bis: capture the base SHA immediately after _init_repo so we can
    # compare after the build to determine if any new commits were created.
    base_sha = _get_head_sha(repo_path)

    # Log path: inside the repo under .cowork/
    log_path = repo_path / ".cowork" / "build.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # 4. Register build_run
    run_id = create_build_run(
        db_path,
        intake_id=intake_id,
        client_id=client_id,
        repo_path=repo_path,
        log_path=log_path,
    )

    # 5. Build command (headless-safe prompt — no /clarify, no enterprise-build)
    prompt = _build_prompt(brief_path, docs_dir, repo_path)
    cmd = ["claude", "-p", prompt]

    # B1-ter: inject ARIS4U_HEADLESS=1 (via os.environ in _headless_launcher below)
    # so hooks that have the guard skip interactive injections (e.g. clarify-gate)
    # during this subprocess.

    # 6. Launch (production = real claude; tests inject fake).
    # The launcher receives the headless env via a wrapper so the production
    # launcher and fake launchers both see it.  We monkey-patch the env into
    # _default_launcher via a closure-compatible pattern: if the caller is
    # the default launcher, we temporarily set the env on the module; otherwise
    # we pass it as a keyword arg if the launcher accepts it, or silently skip.
    # Simpler: wrap the launcher call so env is handled at this level for the
    # production path.  The injected env is tested via the fake launcher's
    # captured call context.
    _log.info("intake %s: launching build run %s (ARIS4U_HEADLESS=1)", intake_id, run_id)

    # The canonical way to pass env to the launcher without changing the Launcher
    # type signature is to wrap _default_launcher here.  For injected fakes,
    # we store the env on the call context by wrapping the launcher transparently.
    def _headless_launcher(
        cmd: list[str], cwd: Path, log_path: Path,
        _inner: Launcher = launcher,
    ) -> int:
        # If the inner launcher is the production default, pass env explicitly.
        # Fake launchers injected in tests receive the call and can inspect
        # that ARIS4U_HEADLESS=1 was present in their environment at call time
        # (we set it in the process env before calling them so os.environ also
        # reflects it, making it fully transparent to fakes).
        import os as _os2
        old = _os2.environ.get("ARIS4U_HEADLESS")
        _os2.environ["ARIS4U_HEADLESS"] = "1"
        try:
            return _inner(cmd, cwd, log_path)
        finally:
            if old is None:
                _os2.environ.pop("ARIS4U_HEADLESS", None)
            else:
                _os2.environ["ARIS4U_HEADLESS"] = old

    returncode = _headless_launcher(cmd, repo_path, log_path)

    # B1-bis: mechanical done criterion — commits trump returncode.
    # returncode 0 + no new commits → needs_review (false-done prevention).
    # returncode != 0                → failed (something went wrong).
    # new commits present            → done (regardless of returncode, as long
    #                                   as returncode != non-zero; if returncode
    #                                   is non-zero AND commits exist we still
    #                                   mark done to not lose real work).
    if base_sha is not None:
        produced = _build_produced_commits(repo_path, base_sha)
    else:
        # Could not capture base SHA (rare git failure): fall back to returncode
        produced = returncode == 0
        _log.warning("intake %s: base_sha unavailable, falling back to returncode", intake_id)

    if produced:
        final_status = "done"
    elif returncode != 0:
        final_status = "failed"
    else:
        # returncode 0 but no new commits → build ran but produced nothing
        final_status = "needs_review"
        _log.warning(
            "intake %s: build exited 0 but produced no new commits — marking needs_review",
            intake_id,
        )

    finish_build_run(db_path, run_id, final_status)
    set_status(db_path, intake_id, final_status)

    _log.info("intake %s: build run %s finished with status %s", intake_id, run_id, final_status)

    return {
        "intake_id": intake_id,
        "slug": slug,
        "repo_path": str(repo_path),
        "run_id": run_id,
        "status": final_status,
        "log_path": str(log_path),
    }


def _read_brief(brief_path: Path) -> str:
    """Read the brief file, returning a placeholder on failure (fail-open).

    Args:
        brief_path: Absolute path to brief.md.

    Returns:
        Brief text or a placeholder string if unreadable.
    """
    try:
        return brief_path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning("no se pudo leer brief en %s (fail-open): %s", brief_path, exc)
        return "# Cowork Project\n\n(brief not readable at build time)"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cowork_runner",
        description="ARIS4U cowork runner — processes one pending intake per call.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Process the oldest pending intake (human-in-the-loop mode).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=_DEFAULT_DB,
        help=f"Path to sessions.db (default: {_DEFAULT_DB})",
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=_DEFAULT_BASE_DIR,
        help=f"Parent directory for greenfield repos (default: {_DEFAULT_BASE_DIR})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns:
        Exit code: 0 on success or no-op, 1 on error.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)

    try:
        result = run_once(args.db, base_dir=args.base_dir)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if result is None:
        print("no pending intakes — nothing to do")
        return 0

    print(
        f"intake_id={result['intake_id']} "
        f"slug={result['slug']} "
        f"repo={result['repo_path']} "
        f"run_id={result['run_id']} "
        f"status={result['status']} "
        f"log={result['log_path']}"
    )
    return 0 if result["status"] == "done" else 1


if __name__ == "__main__":
    sys.exit(main())
