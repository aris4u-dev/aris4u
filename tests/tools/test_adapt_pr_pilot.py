"""Tests for tools/adapt/pr_pilot.py — pr-only autopilot (Tramo 3 §7).

Verifica (sin tocar repos, git ni gh reales):
  - gate falla → sin rama, sin PR, log emitido, exit 1
  - deltas_json vacío / inválido → exit 1, log
  - happy path → branch, commit, push, PR abiertos; log adapt_pr_opened, exit 0
  - git push falla → rollback (checkout + branch -D), log adapt_pr_push_failed, exit 1
  - gh pr create falla → rollback (+ delete remote), log adapt_pr_gh_create_failed, exit 1
  - dry-run → subprocess.run NUNCA llamado, exit 0, log con dry_run=True
  - _build_pr_body → contiene campos obligatorios y advertencia de no-auto-merge
  - _rollback → checkout + branch -D (+ optional remote delete)
  - main() → parsea flags y llama run_pr_pilot correctamente

Patrón: monkeypatch sobre pilot._run (módulo-level, no subprocess.run directamente)
para interceptar todos los git/gh calls. NO se crean PRs ni ramas reales.
"""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest

import tools.adapt.pr_pilot as pilot


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

MINIMAL_DELTAS = json.dumps({
    "changed": True,
    "first_run": False,
    "deltas": [{"source": "claude_version", "route": "mechanical", "old": "1.0.0", "new": "1.1.0"}],
    "mechanical": [{"source": "claude_version", "route": "mechanical", "old": "1.0.0", "new": "1.1.0"}],
    "semantic": [],
    "current": {"claude_version": "1.1.0", "claude_model": "opus-4-8"},
})

SEMANTIC_DELTAS = json.dumps({
    "changed": True,
    "first_run": False,
    "deltas": [{"source": "changelog_hash", "route": "semantic", "old": "aaa", "new": "bbb"}],
    "mechanical": [],
    "semantic": [{"source": "changelog_hash", "route": "semantic", "old": "aaa", "new": "bbb"}],
    "current": {},
})

EMPTY_DELTAS_JSON = json.dumps({
    "changed": False, "first_run": False, "deltas": [], "mechanical": [], "semantic": [], "current": {},
})


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the adapt log to a tmp file; never touch the real logs/adapt.jsonl."""
    monkeypatch.setattr(pilot, "_LOG_FILE", tmp_path / "adapt.jsonl")


def _state_file_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ROOT and create data/adapt_state.json so real-mode checks pass."""
    monkeypatch.setattr(pilot, "ROOT", tmp_path)
    (tmp_path / "data").mkdir(exist_ok=True)
    state = tmp_path / "data" / "adapt_state.json"
    state.write_text("{}")
    return state


def _make_run_mock(*, fail_cmd: str | None = None) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Return a _run replacement that succeeds unless fail_cmd substring is in cmd."""
    def _side(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        if fail_cmd and fail_cmd in " ".join(str(c) for c in cmd):
            raise subprocess.CalledProcessError(1, cmd, output="", stderr="simulated")
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                           stdout="https://github.com/x/y/pull/1", stderr="")
    return _side


def _read_log(log: Path) -> list[dict[str, object]]:
    """Parse a JSONL log file into a list of event dicts; skip blank lines."""
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# _build_pr_body
# ---------------------------------------------------------------------------

class TestBuildPrBody:
    def test_contains_branch_and_gate_pass(self) -> None:
        body = pilot._build_pr_body(json.loads(MINIMAL_DELTAS), gate_pass=True,
                                    branch="adapt/auto-20260101-120000")
        assert "adapt/auto-20260101-120000" in body
        assert "PASS" in body

    def test_contains_delta_details(self) -> None:
        body = pilot._build_pr_body(json.loads(MINIMAL_DELTAS), gate_pass=True, branch="b")
        assert "claude_version" in body
        assert "1.0.0" in body
        assert "1.1.0" in body

    def test_never_automerge_warning_always_present(self) -> None:
        body = pilot._build_pr_body(json.loads(EMPTY_DELTAS_JSON), gate_pass=True, branch="b")
        assert "NUNCA se auto-mergea" in body

    def test_semantic_section_present_for_semantic_deltas(self) -> None:
        body = pilot._build_pr_body(json.loads(SEMANTIC_DELTAS), gate_pass=True, branch="b")
        assert "semánticos" in body.lower()

    def test_mechanical_section_present_for_mechanical_deltas(self) -> None:
        body = pilot._build_pr_body(json.loads(MINIMAL_DELTAS), gate_pass=True, branch="b")
        assert "mecánicos" in body.lower()

    def test_gate_fail_reflected(self) -> None:
        body = pilot._build_pr_body(json.loads(MINIMAL_DELTAS), gate_pass=False, branch="b")
        assert "FAIL" in body


# ---------------------------------------------------------------------------
# _rollback
# ---------------------------------------------------------------------------

class TestRollback:
    def test_calls_checkout_then_delete_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        pilot._rollback("adapt/auto-xyz", "main", False)

        assert any("checkout" in c and "main" in c for c in calls)
        assert any("branch" in c and "-D" in c and "adapt/auto-xyz" in c for c in calls)

    def test_delete_remote_called_when_flag_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        pilot._rollback("adapt/auto-xyz", "main", False, delete_remote=True)

        remote_deletes = [c for c in calls if "push" in c and "--delete" in c]
        assert len(remote_deletes) == 1

    def test_checkout_failure_does_not_abort_branch_delete(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If checkout fails, _rollback still attempts branch -D (fail-open)."""
        call_count = 0

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            nonlocal call_count
            call_count += 1
            if "checkout" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        pilot._rollback("adapt/auto-xyz", "main", False)
        # Both checkout and branch -D must have been attempted despite checkout failing
        assert call_count >= 2


# ---------------------------------------------------------------------------
# run_pr_pilot — input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_empty_deltas_json_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        assert pilot.run_pr_pilot("", gate_pass=True) == 1

    def test_invalid_json_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        assert pilot.run_pr_pilot("{NOT JSON!!!", gate_pass=True) == 1

    def test_invalid_json_logs_event(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "adapt.jsonl"
        monkeypatch.setattr(pilot, "_LOG_FILE", log)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot("{BAD}", gate_pass=True)
        assert any(e["event"] == "adapt_pr_bad_deltas_json" for e in _read_log(log))


# ---------------------------------------------------------------------------
# run_pr_pilot — gate failure
# ---------------------------------------------------------------------------

class TestGateFailure:
    def test_gate_fail_returns_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        monkeypatch.setattr(pilot, "_run", _make_run_mock())
        assert pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=False) == 1

    def test_gate_fail_no_git_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=False)
        assert calls == []

    def test_gate_fail_logs_event(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "adapt.jsonl"
        monkeypatch.setattr(pilot, "_LOG_FILE", log)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=False)
        assert any(e["event"] == "adapt_pr_gate_failed" for e in _read_log(log))


# ---------------------------------------------------------------------------
# run_pr_pilot — happy path
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_returns_0(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _state_file_for(tmp_path, monkeypatch)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        monkeypatch.setattr(pilot, "_run", _make_run_mock())
        assert pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True) == 0

    def test_creates_branch_with_adapt_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _state_file_for(tmp_path, monkeypatch)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0,
                                               stdout="https://github.com/x/y/pull/1", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)

        branch_creates = [c for c in calls if "checkout" in c and "-b" in c]
        assert len(branch_creates) == 1
        assert branch_creates[0][-1].startswith("adapt/auto-")

    def test_calls_gh_pr_create(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _state_file_for(tmp_path, monkeypatch)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            return subprocess.CompletedProcess(args=cmd, returncode=0,
                                               stdout="https://github.com/x/y/pull/1", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)

        gh_calls = [c for c in calls if "gh" in c and "pr" in c and "create" in c]
        assert len(gh_calls) == 1

    def test_logs_adapt_pr_opened(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "adapt.jsonl"
        monkeypatch.setattr(pilot, "_LOG_FILE", log)
        _state_file_for(tmp_path, monkeypatch)
        monkeypatch.setattr(pilot, "_run", _make_run_mock())
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)
        assert any(e["event"] == "adapt_pr_opened" for e in _read_log(log))

    def test_start_event_logged_with_source_info(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "adapt.jsonl"
        monkeypatch.setattr(pilot, "_LOG_FILE", log)
        _state_file_for(tmp_path, monkeypatch)
        monkeypatch.setattr(pilot, "_run", _make_run_mock())
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)
        entries = _read_log(log)
        start = next((e for e in entries if e["event"] == "adapt_pr_start"), None)
        assert start is not None
        assert "claude_version" in str(start.get("sources", ""))


# ---------------------------------------------------------------------------
# run_pr_pilot — push failure → rollback (no remote delete)
# ---------------------------------------------------------------------------

class TestPushFailureRollback:
    def test_returns_1_on_push_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _state_file_for(tmp_path, monkeypatch)
        monkeypatch.setattr(pilot, "_run", _make_run_mock(fail_cmd="--set-upstream"))
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        assert pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True) == 1

    def test_rollback_after_push_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _state_file_for(tmp_path, monkeypatch)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            if "--set-upstream" in cmd:
                raise subprocess.CalledProcessError(1, cmd, output="", stderr="net error")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)

        push_idx = next(i for i, c in enumerate(calls) if "--set-upstream" in c)
        post_push = calls[push_idx + 1:]
        assert any("checkout" in c for c in post_push)
        assert any("-D" in c for c in post_push)

    def test_no_remote_delete_when_push_failed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Push never reached remote → rollback must NOT attempt remote branch delete."""
        _state_file_for(tmp_path, monkeypatch)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            if "--set-upstream" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)

        assert [c for c in calls if "push" in c and "--delete" in c] == []

    def test_push_failure_logged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "adapt.jsonl"
        monkeypatch.setattr(pilot, "_LOG_FILE", log)
        _state_file_for(tmp_path, monkeypatch)
        monkeypatch.setattr(pilot, "_run", _make_run_mock(fail_cmd="--set-upstream"))
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)
        assert any(e["event"] == "adapt_pr_push_failed" for e in _read_log(log))


# ---------------------------------------------------------------------------
# run_pr_pilot — gh pr create failure → rollback (WITH remote delete)
# ---------------------------------------------------------------------------

class TestPrCreateFailureRollback:
    def test_returns_1_on_pr_create_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _state_file_for(tmp_path, monkeypatch)
        monkeypatch.setattr(pilot, "_run", _make_run_mock(fail_cmd="gh pr create"))
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        assert pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True) == 1

    def test_rollback_after_pr_create_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _state_file_for(tmp_path, monkeypatch)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            if "gh" in cmd and "pr" in cmd and "create" in cmd:
                raise subprocess.CalledProcessError(1, cmd, output="", stderr="gh error")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)

        pr_idx = next(i for i, c in enumerate(calls) if "gh" in c and "create" in c)
        assert any("checkout" in c for c in calls[pr_idx + 1:])

    def test_remote_delete_attempted_when_push_succeeded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Push succeeded before PR creation failed → rollback MUST try remote delete."""
        _state_file_for(tmp_path, monkeypatch)
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            if "gh" in cmd and "pr" in cmd and "create" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)

        assert len([c for c in calls if "push" in c and "--delete" in c]) == 1

    def test_pr_create_failure_logged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "adapt.jsonl"
        monkeypatch.setattr(pilot, "_LOG_FILE", log)
        _state_file_for(tmp_path, monkeypatch)
        monkeypatch.setattr(pilot, "_run", _make_run_mock(fail_cmd="gh pr create"))
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)
        assert any(e["event"] == "adapt_pr_gh_create_failed" for e in _read_log(log))


# ---------------------------------------------------------------------------
# run_pr_pilot — branch creation failure (very early; nothing to rollback)
# ---------------------------------------------------------------------------

class TestBranchCreateFailure:
    def test_returns_1_when_checkout_b_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pilot, "_run", _make_run_mock(fail_cmd="-b adapt"))
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        assert pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True) == 1

    def test_no_rollback_when_branch_never_created(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If checkout -b fails, rollback (which would try checkout again) must NOT run."""
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(cmd))
            if "-b" in cmd:
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pilot, "_run", fake_run)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True)

        # Only the failed checkout -b; no subsequent checkout (rollback) should appear
        checkouts = [c for c in calls if "checkout" in c]
        assert all("-b" in c for c in checkouts)


# ---------------------------------------------------------------------------
# run_pr_pilot — dry-run
# ---------------------------------------------------------------------------

class TestDryRun:
    def test_dry_run_returns_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        assert pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True, dry_run=True) == 0

    def test_dry_run_never_calls_subprocess_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """In dry-run mode, subprocess.run must never be invoked."""
        real_run = mock.MagicMock(name="subprocess.run")
        monkeypatch.setattr(pilot.subprocess, "run", real_run)
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True, dry_run=True)
        real_run.assert_not_called()

    def test_dry_run_logs_with_dry_run_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        log = tmp_path / "adapt.jsonl"
        monkeypatch.setattr(pilot, "_LOG_FILE", log)
        monkeypatch.setattr(pilot, "_current_branch", lambda **_kw: "main")
        pilot.run_pr_pilot(MINIMAL_DELTAS, gate_pass=True, dry_run=True)
        entries = _read_log(log)
        assert all(e.get("dry_run") is True for e in entries)
        assert any(e["event"] == "adapt_pr_start" for e in entries)


# ---------------------------------------------------------------------------
# main() — argument parsing
# ---------------------------------------------------------------------------

class TestMain:
    def test_dry_run_flag_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        received: dict[str, object] = {}

        def fake_pilot(deltas_json: str, gate_pass: bool, dry_run: bool = False) -> int:
            received.update({"deltas_json": deltas_json, "gate_pass": gate_pass, "dry_run": dry_run})
            return 0

        monkeypatch.setattr(pilot, "run_pr_pilot", fake_pilot)
        rc = pilot.main(["--deltas-json", MINIMAL_DELTAS, "--gate-pass", "true", "--dry-run"])
        assert rc == 0
        assert received["dry_run"] is True
        assert received["gate_pass"] is True

    def test_gate_pass_false_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        received: dict[str, object] = {}

        def fake_pilot(deltas_json: str, gate_pass: bool, dry_run: bool = False) -> int:
            received["gate_pass"] = gate_pass
            return 1

        monkeypatch.setattr(pilot, "run_pr_pilot", fake_pilot)
        pilot.main(["--gate-pass", "false"])
        assert received["gate_pass"] is False

    def test_default_gate_pass_is_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        received: dict[str, object] = {}

        def fake_pilot(deltas_json: str, gate_pass: bool, dry_run: bool = False) -> int:
            received["gate_pass"] = gate_pass
            return 0

        monkeypatch.setattr(pilot, "run_pr_pilot", fake_pilot)
        pilot.main([])
        assert received["gate_pass"] is True

    def test_returns_pilot_exit_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pilot, "run_pr_pilot", lambda *a, **kw: 1)
        assert pilot.main(["--gate-pass", "false"]) == 1


# ---------------------------------------------------------------------------
# config — AUTOUPDATE_MODE registered
# ---------------------------------------------------------------------------

class TestConfig:
    def test_autoupdate_mode_in_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AUTOUPDATE_MODE must be importable and default to 'shadow'.

        Hermético: aísla env Y el config.json real del usuario (ARIS4U_CONFIG →
        path inexistente) — si el operador activa pr-only en su config, este test
        seguía leyendo su valor real y fallaba (detectado 2026-07-01 al activarlo).
        """
        import importlib

        monkeypatch.delenv("ARIS4U_AUTOUPDATE", raising=False)
        monkeypatch.setenv("ARIS4U_CONFIG", "/nonexistent/aris4u-test-config.json")
        import engine.v16.config as cfg
        importlib.reload(cfg)
        assert hasattr(cfg, "AUTOUPDATE_MODE")
        assert cfg.AUTOUPDATE_MODE == "shadow"
        # Restaura el módulo al estado real para no contaminar otros tests.
        monkeypatch.undo()
        importlib.reload(cfg)

    def test_autoupdate_mode_respects_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        monkeypatch.setenv("ARIS4U_AUTOUPDATE", "pr-only")
        import engine.v16.config as cfg
        importlib.reload(cfg)
        assert cfg.AUTOUPDATE_MODE == "pr-only"
        importlib.reload(cfg)
