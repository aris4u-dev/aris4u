"""Equivalencia PostToolUse: orquestador python (dispatch) vs los hooks .sh viejos.

PostToolUse cablea VARIOS hooks del repo (con matchers por tool). Este test verifica:
  - redact_secrets (Bash): la MUTACIÓN del output (`updatedToolOutput`) es IDÉNTICA al
    .sh viejo — caso crítico. Golden capturado de hooks/redact_secrets.sh.
  - parallel-dispatch-guard (Write .sh): additionalContext idéntico al .sh viejo.
  - capture_commit (Bash + git commit): side-effect (save_decision) ocurre, con dedup.
  - agent_dispatched (Agent/Task): side-effect (línea JSONL con repo_heads_pre).
  - gating: tools no aplicables → no-op (sin stdout).

El JSONL-safe se prueba con un secreto en línea JSONL compacta: el valor se redacta y
la línea sigue siendo JSON válido (keepme sobrevive).

Corre:  .venv312/bin/python3 -m pytest tests/dispatch/test_post_tool_use.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
INVOKE = str(ROOT / "tests" / "dispatch" / "_invoke.py")
FIXDIR = Path(__file__).resolve().parent / "fixtures"
GOLDEN = Path(__file__).resolve().parent / "golden"
HOOKS = ROOT / "hooks"

# Permite importar dispatch.* y engine.* en-proceso para los tests de side-effect.
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run_new(fixture: str) -> dict:
    """Corre el orquestador nuevo vía _invoke; devuelve el JSON de stdout ({} si vacío)."""
    payload = (FIXDIR / fixture).read_text()
    proc = subprocess.run(
        [PY, INVOKE, "post_tool_use", "PostToolUse"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    out = proc.stdout.strip()
    return json.loads(out) if out else {}


def _ac(d: dict) -> str:
    """additionalContext, viva en top-level o dentro de hookSpecificOutput."""
    return d.get("additionalContext") or d.get("hookSpecificOutput", {}).get(
        "additionalContext", ""
    )


def _uo(d: dict):
    """updatedToolOutput dentro de hookSpecificOutput (o None)."""
    return d.get("hookSpecificOutput", {}).get("updatedToolOutput")


# ---------- redact_secrets: mutación EXACTA vs golden del .sh ----------


def test_redact_bash_mutation_matches_golden() -> None:
    new = _run_new("post_tool_use_redact_bash.json")
    old = json.loads((GOLDEN / "post_tool_use_redact_bash.out").read_text())
    assert _uo(new) == _uo(old), "updatedToolOutput diverge del .sh viejo"
    assert _ac(new) == old.get("additionalContext"), "additionalContext diverge"
    assert _uo(new) == (
        "AWS_ACCESS_KEY=[REDACTED:aws_access_key] and [REDACTED:aws_secret] done"
    )


def test_redact_jsonl_safe_keeps_line_valid() -> None:
    new = _run_new("post_tool_use_redact_jsonl.json")
    old = json.loads((GOLDEN / "post_tool_use_redact_jsonl.out").read_text())
    uo = _uo(new)
    assert uo == _uo(old), "mutación JSONL diverge del .sh viejo"
    assert "[REDACTED:aws_secret]" in uo
    assert "keepme" in uo, "el JSONL-safe cut no debe tragarse el resto de la línea"
    assert json.loads(uo), "la línea redactada debe seguir siendo JSON válido"


def test_non_bash_is_not_redacted() -> None:
    # Un Write con apariencia de secreto NO debe redactarse (redact solo procesa Bash).
    payload = json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/tmp/x.txt",
                "content": "aws_secret_access_key=wJalrXUtnFEMIK7MDENGbPxRf",
            },
        }
    )
    proc = subprocess.run(
        [PY, INVOKE, "post_tool_use", "PostToolUse"],
        input=payload,
        capture_output=True,
        text=True,
        timeout=20,
    )
    out = proc.stdout.strip()
    d = json.loads(out) if out else {}
    assert _uo(d) is None, "Write no debe mutar output"


# ---------- parallel-dispatch-guard: additionalContext vs golden ----------


def test_parallel_guard_matches_golden() -> None:
    new = _run_new("post_tool_use_parallel_guard.json")
    old = json.loads((GOLDEN / "post_tool_use_parallel_guard.out").read_text())
    assert _ac(new) == _ac(old)
    assert "PARALLEL DISPATCH: 2 sequential ssh" in _ac(new)


# ---------- capture_commit: side-effect (save_decision con dedup) ----------


def test_capture_commit_side_effect(tmp_path, monkeypatch) -> None:
    """En un repo git temporal, un `git commit` captura UNA decision (idempotente)."""
    from dispatch.handlers import capture_commit

    # Repo git real con un commit.
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    (repo / "f.txt").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-qm", "feat: thing"], cwd=repo, check=True, env=env)

    saved: list = []
    seen: set = set()

    def fake_query_db(sql: str, params: tuple = (), fetch_all: bool = True) -> list:
        return [{"1": 1}] if params and params[0] in seen else []

    def fake_save_decision(**kwargs: object) -> None:
        seen.add(kwargs.get("session_ref"))
        saved.append(kwargs)

    monkeypatch.setattr("engine.v16.session_manager.query_db", fake_query_db)
    monkeypatch.setattr("engine.v16.session_manager.save_decision", fake_save_decision)
    monkeypatch.setattr(
        "engine.v16.session_manager.resolve_client_from_path", lambda p: None
    )

    inp = {"command": "git commit -m 'feat: thing'"}
    capture_commit.run("Bash", inp, str(repo))
    capture_commit.run("Bash", inp, str(repo))  # segunda vez → dedup

    assert len(saved) == 1, f"esperaba 1 decision (dedup), hubo {len(saved)}"
    assert saved[0]["domain"] == "git-commit"
    assert saved[0]["session_ref"]
    assert "feat: thing" in saved[0]["decision"]


def test_capture_commit_skips_non_commit(monkeypatch) -> None:
    from dispatch.handlers import capture_commit

    called = {"n": 0}
    monkeypatch.setattr(
        "engine.v16.session_manager.save_decision",
        lambda **k: called.__setitem__("n", called["n"] + 1),
    )
    capture_commit.run("Bash", {"command": "git status"}, "/tmp")
    capture_commit.run("Write", {"command": "git commit -m x"}, "/tmp")
    assert called["n"] == 0


# ---------- agent_dispatched: side-effect (JSONL) ----------


def test_agent_dispatched_writes_jsonl(tmp_path, monkeypatch) -> None:
    from dispatch.handlers import agent_dispatched

    log = tmp_path / "events.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))

    inp = {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "qa-agent", "prompt": "run tests"},
    }
    agent_dispatched.run("Agent", inp)

    assert log.exists(), "agent_dispatched debe escribir el evento JSONL"
    ev = json.loads(log.read_text().strip().splitlines()[-1])
    assert ev["event"] == "agent_dispatched"
    assert ev["subagent_type"] == "qa-agent"
    assert "repo_heads_pre" in ev


def test_agent_dispatched_noop_without_log(monkeypatch, tmp_path) -> None:
    from dispatch.handlers import agent_dispatched

    monkeypatch.delenv("ARIS4U_VALIDATION_LOG", raising=False)
    log = tmp_path / "x.jsonl"
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))
    agent_dispatched.run("Agent", {"tool_name": "Agent", "tool_input": {}})
    assert not log.exists()


def test_agent_dispatched_skips_non_agent(monkeypatch, tmp_path) -> None:
    from dispatch.handlers import agent_dispatched

    log = tmp_path / "x.jsonl"
    monkeypatch.setenv("ARIS4U_VALIDATION_LOG", "1")
    monkeypatch.setenv("ARIS4U_LOG_FILE", str(log))
    agent_dispatched.run("Bash", {"tool_name": "Bash", "tool_input": {}})
    assert not log.exists()


if __name__ == "__main__":
    sys.exit(subprocess.call([PY, "-m", "pytest", __file__, "-v"]))
