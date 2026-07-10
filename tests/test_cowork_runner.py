"""Tests for tools/cowork_runner.py — increment B1 (headless-safe build).

All tests use tmp_path fixtures and inject a fake launcher.
The real ``claude`` binary is NEVER invoked.

Covers:
- run_once with no pending intakes → no-op, returns None.
- run_once with a pending intake → transitions building→done, creates repo
  with git init, registers build_runs row running→done, log written.
- Fake launcher returning 1 → intake and build_run end as 'failed'.
- Abort when destination repo already exists and is non-empty.
- ensure_build_runs_table idempotent + once-per-process flag.
- list_build_runs filtered and unfiltered.
- B1: prompt content — no /clarify, no enterprise-build; DISCOVER/BUILD/VERIFY
  phases present; commit instruction present; brief_path and repo_path present.
- B1-bis: mechanical done criterion via _build_produced_commits; fake launcher
  that creates a commit → status done; fake that does NOT commit → needs_review
  even when returncode is 0; returncode non-zero + no commits → failed.
- B1-ter: run_once sets ARIS4U_HEADLESS=1 in os.environ before calling launcher;
  clarify-gate returns no-op under ARIS4U_HEADLESS=1.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tools.cowork_runner as _runner_mod  # noqa: E402
from tools.cowork_runner import (  # noqa: E402
    _build_produced_commits,
    _build_prompt,
    _get_head_sha,
    create_build_run,
    ensure_build_runs_table,
    finish_build_run,
    list_build_runs,
    run_once,
)
from tools.cowork_intake import create_intake, ensure_intake_table  # noqa: E402
import tools.cowork_intake as _ci_mod  # noqa: E402  (reset once-per-process flag)


# ---------------------------------------------------------------------------
# Fake launcher factory
# ---------------------------------------------------------------------------

def _make_launcher(returncode: int = 0) -> tuple[Callable[..., int], list[dict]]:
    """Return (fake_launcher, calls_log).

    The fake launcher records every invocation in calls_log and writes a
    sentinel line to the log file so tests can verify it was created.
    It NEVER calls the real ``claude`` binary.

    B1-ter: also records the value of ARIS4U_HEADLESS from os.environ at call
    time, so tests can verify that run_once injects the flag before calling.
    """
    calls: list[dict] = []

    def fake(cmd: list[str], cwd: Path, log_path: Path) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"fake build log rc={returncode}\n", encoding="utf-8")
        calls.append({
            "cmd": cmd,
            "cwd": cwd,
            "log_path": log_path,
            "headless_env": os.environ.get("ARIS4U_HEADLESS"),
        })
        return returncode

    return fake, calls


def _make_committing_launcher(
    repo_path_ref: list[Path],
    returncode: int = 0,
) -> tuple[Callable[..., int], list[dict]]:
    """Return a fake launcher that creates a real git commit in the repo.

    Used to test the B1-bis mechanical done criterion: a build is 'done'
    only when it produces new commits.  repo_path_ref is a single-element
    list so the repo path can be resolved after run_once sets it up.

    It NEVER calls the real ``claude`` binary.
    """
    calls: list[dict] = []
    _git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test Runner",
        "GIT_AUTHOR_EMAIL": "test@aris4u.local",
        "GIT_COMMITTER_NAME": "Test Runner",
        "GIT_COMMITTER_EMAIL": "test@aris4u.local",
    }

    def fake(cmd: list[str], cwd: Path, log_path: Path) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"fake committing build log rc={returncode}\n", encoding="utf-8")
        calls.append({"cmd": cmd, "cwd": cwd, "log_path": log_path})
        # Create a real commit inside the repo so _build_produced_commits fires.
        (cwd / "src").mkdir(exist_ok=True)
        (cwd / "src" / "main.py").write_text("# generated\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(cwd), "add", "-A"],
                       check=True, capture_output=True, env=_git_env)
        subprocess.run(["git", "-C", str(cwd), "commit", "-m", "feat: initial build"],
                       check=True, capture_output=True, env=_git_env)
        return returncode

    return fake, calls


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_flags() -> None:  # return type already correct
    """Reset once-per-process flags so each test starts clean."""
    _ci_mod._INTAKE_TABLE_READY = False
    _runner_mod._BUILD_RUNS_TABLE_READY = False


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    """Isolated SQLite DB in tmp_path."""
    return tmp_path / "sessions.db"


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    """Data directory for intake files."""
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture()
def base_dir(tmp_path: Path) -> Path:
    """Base directory for greenfield repos (test-isolated, NOT ~/projects/05-cowork)."""
    d = tmp_path / "cowork"
    d.mkdir()
    return d


def _seed_intake(  # noqa: E302
    db: Path, data_dir: Path, client: str = "testceo", brief: str = "Build me a SaaS",
) -> int:
    """Create a pending intake and return its row_id."""
    row_id, _ = create_intake(
        db_path=db,
        client_id=client,
        brief_text=brief,
        doc_files=[],
        data_dir=data_dir,
    )
    return row_id


# ---------------------------------------------------------------------------
# ensure_build_runs_table
# ---------------------------------------------------------------------------

class TestEnsureBuildRunsTable:
    def test_creates_table(self, db: Path) -> None:
        ensure_build_runs_table(db)
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='build_runs'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_idempotent(self, db: Path) -> None:
        ensure_build_runs_table(db)
        ensure_build_runs_table(db)  # second call is a no-op via flag
        conn = sqlite3.connect(str(db))
        try:
            count = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='build_runs'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1

    def test_flag_set_after_first_call(self, db: Path) -> None:
        assert not _runner_mod._BUILD_RUNS_TABLE_READY
        ensure_build_runs_table(db)
        assert _runner_mod._BUILD_RUNS_TABLE_READY


# ---------------------------------------------------------------------------
# list_build_runs
# ---------------------------------------------------------------------------

class TestListBuildRuns:
    def test_empty_when_table_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "no.db"
        assert list_build_runs(missing) == []

    def test_returns_all_rows(self, db: Path) -> None:
        ensure_build_runs_table(db)
        create_build_run(db, intake_id=1, client_id="c1",
                         repo_path="/tmp/r1", log_path="/tmp/r1/build.log")
        create_build_run(db, intake_id=2, client_id="c2",
                         repo_path="/tmp/r2", log_path="/tmp/r2/build.log")
        rows = list_build_runs(db)
        assert len(rows) == 2

    def test_filter_by_status(self, db: Path) -> None:
        ensure_build_runs_table(db)
        run_id = create_build_run(db, intake_id=1, client_id="c1",
                                  repo_path="/tmp/r1", log_path="/tmp/r1/b.log")
        finish_build_run(db, run_id, "done")
        create_build_run(db, intake_id=2, client_id="c1",
                         repo_path="/tmp/r2", log_path="/tmp/r2/b.log")

        done_rows = list_build_runs(db, status="done")
        running_rows = list_build_runs(db, status="running")
        assert len(done_rows) == 1
        assert len(running_rows) == 1


# ---------------------------------------------------------------------------
# create_build_run / finish_build_run
# ---------------------------------------------------------------------------

class TestBuildRunCRUD:
    def test_create_returns_run_id(self, db: Path) -> None:
        run_id = create_build_run(db, intake_id=7, client_id="acme",
                                  repo_path="/tmp/r", log_path="/tmp/r/b.log")
        assert isinstance(run_id, int) and run_id >= 1

    def test_initial_status_running(self, db: Path) -> None:
        run_id = create_build_run(db, intake_id=1, client_id="c",
                                  repo_path="/tmp/r", log_path="/tmp/r/b.log")
        rows = list_build_runs(db)
        row = next(r for r in rows if r["run_id"] == run_id)
        assert row["status"] == "running"
        assert row["ended_at"] is None

    def test_finish_done(self, db: Path) -> None:
        run_id = create_build_run(db, intake_id=1, client_id="c",
                                  repo_path="/tmp/r", log_path="/tmp/r/b.log")
        finish_build_run(db, run_id, "done")
        rows = list_build_runs(db)
        row = next(r for r in rows if r["run_id"] == run_id)
        assert row["status"] == "done"
        assert row["ended_at"] is not None

    def test_finish_failed(self, db: Path) -> None:
        run_id = create_build_run(db, intake_id=1, client_id="c",
                                  repo_path="/tmp/r", log_path="/tmp/r/b.log")
        finish_build_run(db, run_id, "failed")
        rows = list_build_runs(db, status="failed")
        assert len(rows) == 1

    def test_finish_invalid_status_raises(self, db: Path) -> None:
        run_id = create_build_run(db, intake_id=1, client_id="c",
                                  repo_path="/tmp/r", log_path="/tmp/r/b.log")
        with pytest.raises(ValueError, match="inválido"):
            finish_build_run(db, run_id, "running")


# ---------------------------------------------------------------------------
# run_once — no pending
# ---------------------------------------------------------------------------

class TestRunOnceNoPending:
    def test_returns_none(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        ensure_intake_table(db)
        fake, calls = _make_launcher()
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is None

    def test_launcher_not_called(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        ensure_intake_table(db)
        fake, calls = _make_launcher()
        run_once(db, launcher=fake, base_dir=base_dir)
        assert calls == []


# ---------------------------------------------------------------------------
# run_once — happy path (returncode 0)
# ---------------------------------------------------------------------------

class TestRunOnceSuccess:
    """Happy-path tests: launcher creates a real commit so done criterion fires."""

    def test_returns_summary_dict(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        _seed_intake(db, data_dir)
        fake, _ = _make_committing_launcher([], returncode=0)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        assert result["status"] == "done"
        assert "intake_id" in result
        assert "run_id" in result
        assert "repo_path" in result
        assert "log_path" in result

    def test_intake_transitions_to_done(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        from tools.cowork_intake import get_intake
        row_id = _seed_intake(db, data_dir)
        fake, _ = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        intake = get_intake(db, row_id)
        assert intake is not None
        assert intake["status"] == "done"

    def test_build_run_registered_as_done(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        _seed_intake(db, data_dir)
        fake, _ = _make_committing_launcher([], returncode=0)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        runs = list_build_runs(db, status="done")
        assert len(runs) == 1
        assert runs[0]["run_id"] == result["run_id"]
        assert runs[0]["ended_at"] is not None

    def test_repo_created_with_git_init(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        _seed_intake(db, data_dir)
        fake, _ = _make_committing_launcher([], returncode=0)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        repo = Path(result["repo_path"])
        assert repo.exists()
        assert (repo / ".git").exists(), ".git directory should exist after git init"
        assert (repo / "README.md").exists()

    def test_log_file_written(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        _seed_intake(db, data_dir)
        fake, _ = _make_committing_launcher([], returncode=0)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        log = Path(result["log_path"])
        assert log.exists()
        assert log.stat().st_size > 0

    def test_launcher_called_exactly_once(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        _seed_intake(db, data_dir)
        fake, calls = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        assert len(calls) == 1

    def test_only_one_intake_processed_per_call(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        """Two pending intakes: run_once processes exactly one."""
        _ci_mod._INTAKE_TABLE_READY = False
        _seed_intake(db, data_dir, client="ceo1", brief="Project Alpha")
        _ci_mod._INTAKE_TABLE_READY = False
        _seed_intake(db, data_dir, client="ceo2", brief="Project Beta")
        fake, calls = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        assert len(calls) == 1
        from tools.cowork_intake import list_intakes
        remaining_pending = list_intakes(db, status="pending")
        assert len(remaining_pending) == 1


# ---------------------------------------------------------------------------
# run_once — failure path (returncode 1)
# ---------------------------------------------------------------------------

class TestRunOnceFailed:
    def test_intake_transitions_to_failed(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        from tools.cowork_intake import get_intake
        row_id = _seed_intake(db, data_dir)
        fake, _ = _make_launcher(1)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        assert result["status"] == "failed"
        intake = get_intake(db, row_id)
        assert intake is not None
        assert intake["status"] == "failed"

    def test_build_run_registered_as_failed(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        _seed_intake(db, data_dir)
        fake, _ = _make_launcher(1)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        runs = list_build_runs(db, status="failed")
        assert len(runs) == 1
        assert runs[0]["run_id"] == result["run_id"]


# ---------------------------------------------------------------------------
# run_once — abort if repo already exists non-empty
# ---------------------------------------------------------------------------

class TestRunOnceAbortExistingRepo:
    def test_raises_runtime_error(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        _seed_intake(db, data_dir)
        fake, calls = _make_launcher(0)

        # Pre-create the repo with content
        from tools.cowork_intake import list_intakes
        from tools.cowork_runner import _make_slug
        pending = list_intakes(db, status="pending")
        intake = pending[-1]
        slug = _make_slug(intake["client_id"], intake["id"])
        existing = base_dir / slug
        existing.mkdir(parents=True)
        (existing / "sentinel.txt").write_text("existing content", encoding="utf-8")

        with pytest.raises(RuntimeError, match="ya existe"):
            run_once(db, launcher=fake, base_dir=base_dir)

    def test_launcher_not_called_on_abort(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        _seed_intake(db, data_dir)
        fake, calls = _make_launcher(0)

        from tools.cowork_intake import list_intakes
        from tools.cowork_runner import _make_slug
        pending = list_intakes(db, status="pending")
        intake = pending[-1]
        slug = _make_slug(intake["client_id"], intake["id"])
        existing = base_dir / slug
        existing.mkdir(parents=True)
        (existing / "sentinel.txt").write_text("block", encoding="utf-8")

        try:
            run_once(db, launcher=fake, base_dir=base_dir)
        except RuntimeError:
            pass
        assert calls == []

    def test_intake_set_failed_on_abort(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        from tools.cowork_intake import get_intake, list_intakes
        from tools.cowork_runner import _make_slug
        row_id = _seed_intake(db, data_dir)
        fake, _ = _make_launcher(0)

        pending = list_intakes(db, status="pending")
        intake = pending[-1]
        slug = _make_slug(intake["client_id"], intake["id"])
        existing = base_dir / slug
        existing.mkdir(parents=True)
        (existing / "existing.txt").write_text("block", encoding="utf-8")

        try:
            run_once(db, launcher=fake, base_dir=base_dir)
        except RuntimeError:
            pass

        item = get_intake(db, row_id)
        assert item is not None
        assert item["status"] == "failed"


# ---------------------------------------------------------------------------
# B1 — Prompt content (headless-safe, no /clarify, no enterprise-build)
# ---------------------------------------------------------------------------

class TestPromptContent:
    """B1: the prompt passed to claude -p must be headless-safe."""

    def test_cmd_includes_brief_path(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        _seed_intake(db, data_dir)
        fake, calls = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        assert len(calls) == 1
        cmd = calls[0]["cmd"]
        assert cmd[0] == "claude"
        assert cmd[1] == "-p"
        assert "brief.md" in cmd[2]

    def test_cmd_includes_repo_path(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        _seed_intake(db, data_dir)
        fake, calls = _make_committing_launcher([], returncode=0)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        assert result["repo_path"] in calls[0]["cmd"][2]

    def test_prompt_does_not_contain_clarify(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        """B1: /clarify must be absent — it hangs in non-interactive mode."""
        _seed_intake(db, data_dir)
        fake, calls = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        prompt = calls[0]["cmd"][2]
        assert "/clarify" not in prompt

    def test_prompt_does_not_contain_enterprise_build(
        self, db: Path, data_dir: Path, base_dir: Path
    ) -> None:
        """B1: enterprise-build Workflow must be absent — not available in -p mode."""
        _seed_intake(db, data_dir)
        fake, calls = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        prompt = calls[0]["cmd"][2]
        assert "enterprise-build" not in prompt

    def test_prompt_contains_discover_phase(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        """B1: DISCOVER phase must be present as direct instruction."""
        _seed_intake(db, data_dir)
        fake, calls = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        assert "DISCOVER" in calls[0]["cmd"][2].upper()

    def test_prompt_contains_build_phase(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        """B1: BUILD phase must be present."""
        _seed_intake(db, data_dir)
        fake, calls = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        assert "BUILD" in calls[0]["cmd"][2].upper()

    def test_prompt_contains_verify_phase(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        """B1: VERIFY phase must be present."""
        _seed_intake(db, data_dir)
        fake, calls = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        assert "VERIFY" in calls[0]["cmd"][2].upper()

    def test_prompt_instructs_commit(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        """B1: prompt must tell claude to commit — progress is git commits."""
        _seed_intake(db, data_dir)
        fake, calls = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        assert "commit" in calls[0]["cmd"][2].lower()

    def test_prompt_headless_no_questions(self, db: Path, data_dir: Path, base_dir: Path) -> None:
        """B1: prompt must instruct claude never to ask questions."""
        _seed_intake(db, data_dir)
        fake, calls = _make_committing_launcher([], returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        prompt = calls[0]["cmd"][2].upper()
        assert "HEADLESS" in prompt or "NON-INTERACTIVE" in prompt

    def test_build_prompt_direct(self, tmp_path: Path) -> None:
        """B1: _build_prompt standalone — verify absence of banned tokens."""
        brief = tmp_path / "brief.md"
        brief.write_text("# Brief\nBuild a SaaS", encoding="utf-8")
        docs = tmp_path / "docs"
        docs.mkdir()
        repo = tmp_path / "repo"
        prompt = _build_prompt(brief, docs, repo)
        assert "/clarify" not in prompt
        assert "enterprise-build" not in prompt
        assert "DISCOVER" in prompt.upper()
        assert "BUILD" in prompt.upper()
        assert "VERIFY" in prompt.upper()
        assert "commit" in prompt.lower()
        assert str(repo) in prompt


# ---------------------------------------------------------------------------
# B1-bis — Mechanical done criterion (_build_produced_commits)
# ---------------------------------------------------------------------------

def _init_tmp_repo(path: Path) -> str:
    """Create a minimal git repo and return the initial HEAD SHA."""
    _git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "t@t.local",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "t@t.local",
    }
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
    (path / "README.md").write_text("init", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "chore: initial"],
                   check=True, capture_output=True, env=_git_env)
    sha = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return sha


class TestBuildProducedCommits:
    """B1-bis: _build_produced_commits is the mechanical done gate."""

    def test_no_new_commits_returns_false(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        base_sha = _init_tmp_repo(repo)
        assert _build_produced_commits(repo, base_sha) is False

    def test_new_commit_returns_true(self, tmp_path: Path) -> None:
        _git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@t.local",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@t.local",
        }
        repo = tmp_path / "repo"
        base_sha = _init_tmp_repo(repo)
        # Add a second commit
        (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "src.py"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "feat: add src"],
                       check=True, capture_output=True, env=_git_env)
        assert _build_produced_commits(repo, base_sha) is True

    def test_invalid_sha_returns_false(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_tmp_repo(repo)
        assert _build_produced_commits(repo, "deadbeefdeadbeef" * 2) is False

    def test_nonexistent_repo_returns_false(self, tmp_path: Path) -> None:
        assert _build_produced_commits(tmp_path / "no-repo", "abc123") is False


class TestRunOnceMechanicalDone:
    """B1-bis: run_once status is driven by commits, not returncode."""

    def test_committing_launcher_rc0_yields_done(
        self, db: Path, data_dir: Path, base_dir: Path
    ) -> None:
        """Launcher creates a commit and exits 0 → status done."""
        _seed_intake(db, data_dir)
        repo_ref: list[Path] = []
        fake, _ = _make_committing_launcher(repo_ref, returncode=0)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        assert result["status"] == "done"

    def test_non_committing_launcher_rc0_yields_needs_review(
        self, db: Path, data_dir: Path, base_dir: Path
    ) -> None:
        """Launcher exits 0 but creates NO commits → needs_review (not done)."""
        _seed_intake(db, data_dir)
        fake, _ = _make_launcher(returncode=0)  # standard fake: no commits
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        assert result["status"] == "needs_review"

    def test_non_committing_launcher_rc1_yields_failed(
        self, db: Path, data_dir: Path, base_dir: Path
    ) -> None:
        """Launcher exits 1 and creates NO commits → failed."""
        _seed_intake(db, data_dir)
        fake, _ = _make_launcher(returncode=1)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        assert result["status"] == "failed"

    def test_committing_launcher_rc1_still_yields_done(
        self, db: Path, data_dir: Path, base_dir: Path
    ) -> None:
        """Launcher commits real work but exits 1 → still done (work was produced)."""
        _seed_intake(db, data_dir)
        repo_ref: list[Path] = []
        fake, _ = _make_committing_launcher(repo_ref, returncode=1)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        assert result["status"] == "done"

    def test_needs_review_intake_reflects_status(
        self, db: Path, data_dir: Path, base_dir: Path
    ) -> None:
        """When status is needs_review, the intake row also reflects it."""
        from tools.cowork_intake import get_intake
        row_id = _seed_intake(db, data_dir)
        fake, _ = _make_launcher(returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        intake = get_intake(db, row_id)
        assert intake is not None
        assert intake["status"] == "needs_review"

    def test_needs_review_build_run_registered(
        self, db: Path, data_dir: Path, base_dir: Path
    ) -> None:
        """needs_review build_run appears in list_build_runs."""
        _seed_intake(db, data_dir)
        fake, _ = _make_launcher(returncode=0)
        result = run_once(db, launcher=fake, base_dir=base_dir)
        assert result is not None
        # needs_review is a valid final status stored in build_runs
        all_runs = list_build_runs(db)
        run = next(r for r in all_runs if r["run_id"] == result["run_id"])
        assert run["status"] == "needs_review"
        assert run["ended_at"] is not None


class TestGetHeadSha:
    """_get_head_sha helper used by run_once for the base SHA snapshot."""

    def test_returns_sha_string(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_tmp_repo(repo)
        sha = _get_head_sha(repo)
        assert sha is not None
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_returns_none_for_nonexistent_repo(self, tmp_path: Path) -> None:
        assert _get_head_sha(tmp_path / "no-repo") is None


# ---------------------------------------------------------------------------
# B1-ter — ARIS4U_HEADLESS=1 injected into launcher environment
# ---------------------------------------------------------------------------

class TestHeadlessEnvInjection:
    """B1-ter: run_once must set ARIS4U_HEADLESS=1 before calling the launcher."""

    def test_headless_env_set_during_launcher_call(
        self, db: Path, data_dir: Path, base_dir: Path
    ) -> None:
        """The fake launcher observes ARIS4U_HEADLESS=1 in os.environ."""
        _seed_intake(db, data_dir)
        fake, calls = _make_launcher(returncode=0)
        # Ensure env var is absent before the call
        os.environ.pop("ARIS4U_HEADLESS", None)
        run_once(db, launcher=fake, base_dir=base_dir)
        assert len(calls) == 1
        assert calls[0]["headless_env"] == "1", (
            "ARIS4U_HEADLESS must be '1' in os.environ during the launcher call"
        )

    def test_headless_env_restored_after_run_once(
        self, db: Path, data_dir: Path, base_dir: Path
    ) -> None:
        """After run_once returns, ARIS4U_HEADLESS must be restored to its prior state."""
        _seed_intake(db, data_dir)
        os.environ.pop("ARIS4U_HEADLESS", None)
        fake, _ = _make_launcher(returncode=0)
        run_once(db, launcher=fake, base_dir=base_dir)
        assert "ARIS4U_HEADLESS" not in os.environ

    def test_headless_env_restored_when_preexisting(
        self, db: Path, data_dir: Path, base_dir: Path
    ) -> None:
        """If ARIS4U_HEADLESS was already set, its original value is restored."""
        _seed_intake(db, data_dir)
        os.environ["ARIS4U_HEADLESS"] = "previous"
        try:
            fake, _ = _make_launcher(returncode=0)
            run_once(db, launcher=fake, base_dir=base_dir)
            assert os.environ.get("ARIS4U_HEADLESS") == "previous"
        finally:
            os.environ.pop("ARIS4U_HEADLESS", None)


# ---------------------------------------------------------------------------
# B1-ter — clarify-gate no-op under ARIS4U_HEADLESS=1
# ---------------------------------------------------------------------------

class TestClarifyGateHeadlessGuard:
    """B1-ter: clarify-gate must be silent when ARIS4U_HEADLESS=1."""

    def _run_gate(self, prompt: str, headless: bool) -> tuple[int, str]:
        """Run the versioned clarify-gate.py and return (returncode, stdout)."""
        gate = (
            Path(__file__).resolve().parent.parent
            / "hooks" / "standalone" / "clarify-gate.py"
        )
        env = dict(os.environ)
        if headless:
            env["ARIS4U_HEADLESS"] = "1"
        else:
            env.pop("ARIS4U_HEADLESS", None)
        payload = __import__("json").dumps({"prompt": prompt})
        result = subprocess.run(
            [sys.executable, str(gate)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
        return result.returncode, result.stdout

    def test_gate_fires_without_headless(self) -> None:
        """Sanity: gate produces output for a build prompt in normal mode."""
        prompt = "build me a SaaS platform for enterprise customers"
        rc, out = self._run_gate(prompt, headless=False)
        assert rc == 0
        # May or may not fire (anti-nag marker could exist), but must not crash.

    def test_gate_noop_under_headless(self) -> None:
        """B1-ter: gate produces NO output when ARIS4U_HEADLESS=1."""
        prompt = "build me a SaaS platform for enterprise customers"
        rc, out = self._run_gate(prompt, headless=True)
        assert rc == 0
        assert out.strip() == "", (
            f"clarify-gate must produce no output under ARIS4U_HEADLESS=1, got: {out!r}"
        )
