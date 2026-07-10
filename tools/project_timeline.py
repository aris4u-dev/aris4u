"""Project timeline: git commits (WHAT) annotated with ARIS4U intent layer (WHY).

Anti-Goodhart principle: git is the anchor.  A commit in git that has no
matching ARIS4U row gets an empty ``why`` dict — it is still shown.  An
ARIS4U row (decision/digest/gate) that cannot be correlated to any commit
by a temporal window shared with a real commit is an orphan and is NEVER
promoted to a standalone timeline entry.  Progress only flows from git,
never from ARIS4U alone.

Correlation strategy (no dedicated commit-SHA column exists in decisions,
digests, or gate_results — confirmed by schema inspection 2026-07-07):

  - decisions:   created_at (TIMESTAMP), session_ref (opaque TEXT, not parseable)
  - digests:     created_at (TIMESTAMP), date (TEXT "YYYY-MM-DD")
  - gate_results: timestamp (ISO TEXT), created_at (TIMESTAMP)

None of these tables stores a commit SHA directly.  Correlation is therefore
by temporal window: for each ARIS4U row we find the commit whose author-date
is closest within a ±window.  Rows that do not fall inside any commit window
are orphans.

gate_results has no client_id column.  Without an additional ``session_ref``
filter all gate rows are loaded globally (across all clients).  Callers that
need per-client gates must post-filter on session_ref.  The CLI requires
``--client`` to be explicit so that cross-client leakage is never silent.

Temporal window: 6 hours on each side of the commit timestamp.  This is
tight enough to avoid spurious matches across unrelated work periods, and
generous enough to absorb clock skew between the DB writer and git.

CLI usage:
    python3 tools/project_timeline.py --repo . --client aris4u
    python3 tools/project_timeline.py --repo . --client aris4u --db data/sessions.db
    python3 tools/project_timeline.py --repo . --client aris4u \\
        add-comment --sha <SHA> --author me --role dev --body "looks good"
    python3 tools/project_timeline.py --repo . --client aris4u \\
        list-comments --sha <SHA>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WINDOW_HOURS = 6  # temporal correlation half-window
_DEFAULT_DB = Path("data") / "sessions.db"

# ---------------------------------------------------------------------------
# Schema migration — cowork_comments table
# ---------------------------------------------------------------------------


def ensure_comments_table(db_path: str | Path) -> None:
    """Create cowork_comments table if it does not exist (idempotent).

    Opens the DB in read-write mode.  Safe to call multiple times; uses
    CREATE TABLE IF NOT EXISTS so it never errors on an already-initialised
    database and never duplicates schema.

    Args:
        db_path: Filesystem path to the SQLite file.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cowork_comments (
                id         INTEGER PRIMARY KEY,
                commit_sha TEXT    NOT NULL,
                author     TEXT    NOT NULL,
                role       TEXT    NOT NULL,
                body       TEXT    NOT NULL,
                client_id  TEXT,
                created_at TEXT    NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Comment CRUD
# ---------------------------------------------------------------------------


def add_comment(
    db_path: str | Path,
    commit_sha: str,
    author: str,
    role: str,
    body: str,
    client_id: str = "",
) -> int:
    """Insert a comment anchored to a commit SHA.

    Calls ensure_comments_table before inserting so the table always exists.

    Args:
        db_path: Path to the SQLite file.
        commit_sha: Full or abbreviated git commit SHA.
        author: Commenter name or identifier.
        role: Role label (e.g. "dev", "reviewer", "qa").
        body: Comment text.
        client_id: Optional client scope tag.

    Returns:
        The ``id`` (rowid) of the newly inserted row.
    """
    ensure_comments_table(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            INSERT INTO cowork_comments (commit_sha, author, role, body, client_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (commit_sha, author, role, body, client_id or None, now),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def list_comments(db_path: str | Path, commit_sha: str) -> list[dict]:
    """Return all comments for a given commit SHA, oldest first.

    Opens the DB in read-only mode (file:...?mode=ro).  Does NOT create the
    DB or the table — if the file does not exist or the cowork_comments table
    is absent, returns [] without side-effects.

    Args:
        db_path: Path to the SQLite file.
        commit_sha: Commit SHA to filter by.

    Returns:
        List of dicts with keys: id, commit_sha, author, role, body,
        client_id, created_at.  Empty list if no comments or table absent.
    """
    conn = _connect_ro(Path(db_path))
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT id, commit_sha, author, role, body, client_id, created_at
            FROM   cowork_comments
            WHERE  commit_sha = ?
            ORDER  BY id ASC
            """,
            (commit_sha,),
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        # Table does not exist yet — no comments.
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _run(cmd: list[str], cwd: Path) -> str:
    """Run a subprocess command and return decoded stdout (empty on error)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout if result.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def _parse_commits(repo_path: Path) -> list[dict]:
    """Return commits from ``git log`` as a list of dicts.

    Each dict has keys: sha, author, date (ISO-8601 string), subject, files.
    An empty list is returned for repos with no commits or when git is absent.

    Note: \x1f (unit-separator) is used as the field delimiter in --format.
    This character should not appear in commit subjects in normal usage; if it
    does, the subject will be truncated at the first occurrence.
    """
    # Separator unlikely to appear in commit messages.
    sep = "\x1f"
    fmt = f"%H{sep}%an{sep}%aI{sep}%s"
    raw = _run(["git", "log", f"--format={fmt}", "--name-only"], repo_path)
    if not raw.strip():
        return []

    commits: list[dict] = []
    current: dict | None = None

    for line in raw.splitlines():
        if sep in line:
            # Header line: flush previous commit, start new one.
            parts = line.split(sep, 3)
            if len(parts) < 4:
                continue
            if current is not None:
                commits.append(current)
            current = {
                "sha": parts[0].strip(),
                "author": parts[1].strip(),
                "date": parts[2].strip(),
                "subject": parts[3].strip(),
                "files": [],
            }
        elif line.strip() and current is not None:
            # File path from --name-only block (blank lines separate commits).
            current["files"].append(line.strip())

    if current is not None:
        commits.append(current)

    return commits


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO-8601 or YYYY-MM-DD string into a UTC-aware datetime.

    Uses datetime.fromisoformat() for full ISO-8601 support (handles offsets,
    microseconds, and the compact "2026-07-01" date-only form).  Falls back to
    explicit strptime for the "YYYY-MM-DD" form on Python < 3.11 where
    fromisoformat does not support all ISO variants.

    Returns None if parsing fails or value is None/empty.
    """
    if not value:
        return None
    # Normalise the common SQLite format "2026-07-01 12:34:56" (space separator)
    # to the ISO delimiter so fromisoformat handles it on all Python 3.11+ builds.
    normalised = value.strip().replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    # Explicit date-only fallback (YYYY-MM-DD).
    try:
        dt = datetime.strptime(normalised[:10], "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# ARIS4U data readers (read-only)
# ---------------------------------------------------------------------------


def _connect_ro(db_path: Path) -> sqlite3.Connection | None:
    """Open a SQLite DB in read-only URI mode.  None if absent or broken.

    Reuses the exact pattern from console/aris4u_console/live_data.py.
    """
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _fetch_decisions(conn: sqlite3.Connection, client_id: str) -> list[dict]:
    """Fetch decisions filtered by client_id (or all if client_id is empty)."""
    if client_id:
        rows = conn.execute(
            "SELECT id, decision, rationale, domain, session_ref, created_at "
            "FROM decisions WHERE client_id = ? ORDER BY id ASC",
            (client_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, decision, rationale, domain, session_ref, created_at "
            "FROM decisions ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_digests(conn: sqlite3.Connection, client_id: str) -> list[dict]:
    """Fetch digests filtered by client_id (or all if client_id is empty)."""
    if client_id:
        rows = conn.execute(
            "SELECT id, date, summary, built, session_id, created_at "
            "FROM digests WHERE client_id = ? ORDER BY created_at ASC",
            (client_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, date, summary, built, session_id, created_at "
            "FROM digests ORDER BY created_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_gates(conn: sqlite3.Connection) -> list[dict]:
    """Fetch all gate_results.

    Note: gate_results has no client_id column.  All rows are returned
    regardless of client scope.  Callers that need per-client filtering must
    post-filter on session_ref (which encodes the session UUID and can be
    correlated to the originating client by the caller).
    """
    rows = conn.execute(
        "SELECT id, module_name, status, details, session_ref, timestamp, created_at "
        "FROM gate_results ORDER BY id ASC"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Temporal correlation
# ---------------------------------------------------------------------------


def _timestamp_for_row(row: dict, date_key: str) -> datetime | None:
    """Extract the best available timestamp from an ARIS4U row.

    Only one key is attempted.  The caller is responsible for choosing the
    most precise available field.  session_ref is NOT a timestamp and is
    never used as a fallback (it is an opaque UUID string).
    """
    return _parse_dt(row.get(date_key))


def _correlate(
    commits: list[dict],
    decisions: list[dict],
    digests: list[dict],
    gates: list[dict],
    window_hours: int = _WINDOW_HOURS,
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """Match ARIS4U rows to commits by temporal window, closest-commit wins.

    For each ARIS4U row the commit whose author-date is closest in time
    (within the window) receives the row.  This prevents all rows from
    piling up on the newest commit when multiple commits fall within the
    same window (dense-session robustness).

    Returns:
        A 4-tuple:
          - annotated commits (each with a populated ``why`` dict)
          - orphan decisions (no commit within window)
          - orphan digests
          - orphan gates
    """
    window = timedelta(hours=window_hours)

    # Parse commit dates once.
    commit_dts: list[datetime | None] = [_parse_dt(c["date"]) for c in commits]

    # Build per-commit buckets.
    commit_decisions: list[list[dict]] = [[] for _ in commits]
    commit_digests: list[list[dict]] = [[] for _ in commits]
    commit_gates: list[list[dict]] = [[] for _ in commits]

    orphan_decisions: list[dict] = []
    orphan_digests: list[dict] = []
    orphan_gates: list[dict] = []

    def _assign(
        rows: list[dict],
        buckets: list[list[dict]],
        orphans: list[dict],
        date_key: str,
    ) -> None:
        """Assign each row to the closest commit within the window."""
        for row in rows:
            row_dt = _timestamp_for_row(row, date_key)
            if row_dt is None:
                orphans.append(row)
                continue

            best_idx: int | None = None
            best_delta = timedelta.max

            for i, cdt in enumerate(commit_dts):
                if cdt is None:
                    continue
                delta = abs(row_dt - cdt)
                if delta <= window and delta < best_delta:
                    best_delta = delta
                    best_idx = i

            if best_idx is not None:
                buckets[best_idx].append(row)
            else:
                orphans.append(row)

    # decisions: created_at is the only real timestamp; session_ref is an
    # opaque UUID — not a parseable timestamp, so no fallback is used.
    _assign(decisions, commit_decisions, orphan_decisions, "created_at")
    # digests: prefer created_at (full precision) over date (day-only → midnight UTC).
    _assign(digests, commit_digests, orphan_digests, "created_at")
    _assign(gates, commit_gates, orphan_gates, "timestamp")

    # Build annotated commits.
    annotated: list[dict] = []
    for i, commit in enumerate(commits):
        entry: dict = {
            "sha": commit["sha"],
            "author": commit["author"],
            "date": commit["date"],
            "subject": commit["subject"],
            "files": commit["files"],
            "why": {
                "decisions": commit_decisions[i],
                "digests": commit_digests[i],
                "gates": commit_gates[i],
            },
        }
        annotated.append(entry)

    return annotated, orphan_decisions, orphan_digests, orphan_gates


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def active_builds(db_path: str | Path, client_id: str) -> list[dict]:
    """Return running build_runs for a specific client with log tail.

    Ephemeral read: only returns rows with status='running' scoped strictly
    to the given client_id.  Does NOT mix with build_timeline — commits are
    the truth; this is transient state only.

    Args:
        db_path: Path to sessions.db.
        client_id: ARIS4U client scope.  Required to prevent cross-client
            leakage (a build for another client NEVER appears).

    Returns:
        List of dicts with keys: run_id, repo_path, started_at, status,
        log_tail (list of str, last ~15 lines of build.log, or []).
        Empty list if table/file absent, or no running builds for this client.
    """
    db_path = Path(db_path)
    conn = _connect_ro(db_path)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT run_id, repo_path, log_path, started_at, status "
            "FROM build_runs "
            "WHERE client_id = ? AND status = 'running' "
            "ORDER BY started_at DESC",
            (client_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        # build_runs table does not exist yet.
        return []
    finally:
        conn.close()

    result: list[dict] = []
    for row in rows:
        log_tail = _read_log_tail(Path(row["log_path"]), lines=15)
        result.append(
            {
                "run_id": row["run_id"],
                "repo_path": row["repo_path"],
                "started_at": row["started_at"],
                "status": row["status"],
                "log_tail": log_tail,
            }
        )
    return result


def _read_log_tail(log_path: Path, lines: int = 15) -> list[str]:
    """Return the last ``lines`` lines of a log file, or [] on any error.

    Read-only, fail-open: if the file is absent, unreadable, or empty,
    returns an empty list without raising.

    Args:
        log_path: Absolute path to the build log file.
        lines: Maximum number of lines to return from the tail.

    Returns:
        List of stripped line strings (at most ``lines`` items).
    """
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    all_lines = text.splitlines()
    return all_lines[-lines:] if all_lines else []


def build_timeline(
    repo_path: str | Path,
    client_id: str,
    db_path: str | Path | None = None,
) -> list[dict]:
    """Build the annotated project timeline.

    Git commits are the anchor.  Each entry represents one commit enriched
    with the ARIS4U intent rows that fall within a ±6-hour temporal window.
    Commits without matching ARIS4U rows are included with an empty ``why``.
    ARIS4U rows that do not correlate to any commit are silently excluded
    (anti-Goodhart: no standalone progress entries from ARIS4U alone).

    If sessions.db is absent, unreadable, or missing the expected tables the
    function degrades gracefully: commits are returned with empty ``why``
    dicts rather than crashing.

    Note on gate_results scope: the gate_results table has no client_id
    column.  All gate rows are returned globally.  If per-client gate
    filtering is needed, post-filter the ``why.gates`` list on ``session_ref``
    in the returned entries.

    Args:
        repo_path: Path to the git repository root.
        client_id: ARIS4U client scope for decisions/digests (e.g. "aris4u").
            An empty string fetches all clients for those tables.
        db_path: Path to sessions.db.  Defaults to ``data/sessions.db``
            relative to ``repo_path`` if not provided.

    Returns:
        List of commit dicts, most-recent first (mirrors ``git log`` order),
        each with keys: sha, author, date, subject, files, why.
        ``why`` contains: decisions, digests, gates (each a list of dicts).
    """
    repo_path = Path(repo_path).resolve()

    if db_path is None:
        db_path = repo_path / _DEFAULT_DB
    else:
        db_path = Path(db_path).resolve()

    commits = _parse_commits(repo_path)
    if not commits:
        return []

    def _empty_why(c: dict) -> dict:
        return {
            "sha": c["sha"],
            "author": c["author"],
            "date": c["date"],
            "subject": c["subject"],
            "files": c["files"],
            "why": {"decisions": [], "digests": [], "gates": []},
        }

    conn = _connect_ro(db_path)
    if conn is None:
        # DB absent or unreadable — return commits with empty why.
        return [_empty_why(c) for c in commits]

    try:
        decisions = _fetch_decisions(conn, client_id)
        digests = _fetch_digests(conn, client_id)
        gates = _fetch_gates(conn)
    except sqlite3.OperationalError:
        # DB exists but is missing expected tables (e.g. freshly created file).
        # Degrade gracefully rather than crashing.  ``finally`` closes conn.
        return [_empty_why(c) for c in commits]
    finally:
        conn.close()

    annotated = _correlate(commits, decisions, digests, gates)[0]
    return annotated


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="project_timeline",
        description=(
            "ARIS4U project timeline: git commits annotated with intent. "
            "--client is required to prevent silent cross-client data leakage."
        ),
    )
    parser.add_argument("--repo", default=".", help="Path to git repo root (default: .)")
    # --client is required: an empty scope would silently dump all clients'
    # decisions/digests, and gate_results has no client_id filter at all.
    # Requiring the flag makes the scope explicit and auditable.
    parser.add_argument(
        "--client",
        required=True,
        help="ARIS4U client_id scope (e.g. 'aris4u', 'client-b'). Required.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to sessions.db (default: <repo>/data/sessions.db)",
    )

    sub = parser.add_subparsers(dest="command")

    # add-comment subcommand
    add_cmd = sub.add_parser("add-comment", help="Add a comment to a commit")
    add_cmd.add_argument("--sha", required=True, help="Commit SHA")
    add_cmd.add_argument("--author", required=True, help="Comment author")
    add_cmd.add_argument("--role", default="dev", help="Author role (default: dev)")
    add_cmd.add_argument("--body", required=True, help="Comment text")

    # list-comments subcommand
    list_cmd = sub.add_parser("list-comments", help="List comments for a commit")
    list_cmd.add_argument("--sha", required=True, help="Commit SHA")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for CLI invocation.

    Args:
        argv: Argument list (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 = success).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    db = Path(args.db).resolve() if args.db else repo / _DEFAULT_DB

    if args.command == "add-comment":
        row_id = add_comment(
            db_path=db,
            commit_sha=args.sha,
            author=args.author,
            role=args.role,
            body=args.body,
            client_id=args.client,
        )
        print(json.dumps({"id": row_id, "sha": args.sha}, indent=2))
        return 0

    if args.command == "list-comments":
        comments = list_comments(db_path=db, commit_sha=args.sha)
        print(json.dumps(comments, indent=2, ensure_ascii=False))
        return 0

    # Default: build and print timeline
    timeline = build_timeline(repo_path=repo, client_id=args.client, db_path=db)
    print(json.dumps(timeline, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
