"""Coverage tests for engine/v16/agent_orchestrator.py.

Exercises the V15 subagent DAG orchestrator: AgentDef / AgentResult / AgentState
dataclasses, dependency-aware wave scheduling (get_waves), readiness resolution
(get_ready), failure/retry bookkeeping (mark_failed + AgentDef.retries),
validation, summary, reset, state persistence roundtrip, and fail-open
degradation when the session_manager backend is unavailable or corrupt.

Direct-import pattern (matches tests/engine/test_soft_reward.py) via the
``engine.v16`` package path so the module's relative ``.session_manager`` import
keeps working. All state lives under ``tmp_path``; the autouse conftest fixtures
(_isolate_sessions_db / _isolate_event_log) redirect the real DB + event log, so
nothing here touches ~/.claude-mem, data/sessions.db, or the real /tmp state file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from engine.v16.agent_orchestrator import (
    AgentDef,
    AgentOrchestrator,
    AgentResult,
    AgentState,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def state_file(tmp_path: Path) -> Path:
    """A fresh, per-test state file path under tmp_path (never the real /tmp one)."""
    return tmp_path / "agent_state.json"


@pytest.fixture
def orch(state_file: Path) -> AgentOrchestrator:
    """A clean orchestrator bound to an isolated state file.

    Because the conftest ``_isolate_sessions_db`` fixture points
    session_manager at a fresh tmp DB, ``_load()`` finds no prior DB state and
    the file does not yet exist, so the orchestrator starts empty.
    """
    return AgentOrchestrator(state_file=str(state_file))


# ---------------------------------------------------------------------------
# Dataclasses / enum
# ---------------------------------------------------------------------------


def test_agentstate_values() -> None:
    """AgentState enum carries the expected lifecycle values."""
    assert AgentState.PENDING.value == "pending"
    assert AgentState.RUNNING.value == "running"
    assert AgentState.COMPLETED.value == "completed"
    assert AgentState.FAILED.value == "failed"
    # round-trip through value (used by _load to rehydrate state)
    assert AgentState("failed") is AgentState.FAILED


def test_agentdef_defaults() -> None:
    """AgentDef defaults: empty deps list, 2 retries, distinct per instance."""
    a = AgentDef(name="x", domain="testing")
    assert a.dependencies == []
    assert a.retries == 2
    # default_factory must not share the same list across instances
    b = AgentDef(name="y", domain="testing")
    a.dependencies.append("z")
    assert b.dependencies == []


def test_agentdef_explicit_retries() -> None:
    """AgentDef accepts explicit retries + dependencies."""
    a = AgentDef(name="x", domain="t", dependencies=["dep"], retries=5)
    assert a.dependencies == ["dep"]
    assert a.retries == 5


def test_agentresult_defaults() -> None:
    """AgentResult starts PENDING with no error."""
    r = AgentResult(agent_name="x")
    assert r.state == AgentState.PENDING
    assert r.error is None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_adds_agent_and_result(orch: AgentOrchestrator) -> None:
    """register() adds the AgentDef and a PENDING AgentResult."""
    orch.register(AgentDef(name="a", domain="test"))
    assert "a" in orch.agents
    assert orch.agents["a"].domain == "test"
    assert "a" in orch.results
    assert orch.results["a"].state == AgentState.PENDING
    assert orch.results["a"].agent_name == "a"


def test_register_allows_forward_reference(orch: AgentOrchestrator) -> None:
    """register() does not validate deps at registration (forward refs allowed)."""
    # 'b' is not registered yet — must not raise.
    orch.register(AgentDef(name="a", domain="test", dependencies=["b"]))
    assert orch.agents["a"].dependencies == ["b"]


# ---------------------------------------------------------------------------
# get_waves — DAG scheduling
# ---------------------------------------------------------------------------


def test_get_waves_empty(orch: AgentOrchestrator) -> None:
    """No agents -> no waves."""
    assert orch.get_waves() == []


def test_get_waves_independent_single_wave(orch: AgentOrchestrator) -> None:
    """Independent agents (no deps) all schedule in one wave, sorted."""
    orch.register(AgentDef(name="c", domain="t"))
    orch.register(AgentDef(name="a", domain="t"))
    orch.register(AgentDef(name="b", domain="t"))
    waves = orch.get_waves()
    assert len(waves) == 1
    assert waves[0] == ["a", "b", "c"]  # sorted within a wave


def test_get_waves_linear_chain(orch: AgentOrchestrator) -> None:
    """Linear chain a->b->c produces three ordered waves."""
    orch.register(AgentDef(name="a", domain="t"))
    orch.register(AgentDef(name="b", domain="t", dependencies=["a"]))
    orch.register(AgentDef(name="c", domain="t", dependencies=["b"]))
    waves = orch.get_waves()
    assert waves == [["a"], ["b"], ["c"]]


def test_get_waves_diamond(orch: AgentOrchestrator) -> None:
    """Diamond a->[b,c]->d collapses b and c into the same middle wave."""
    orch.register(AgentDef(name="a", domain="t"))
    orch.register(AgentDef(name="b", domain="t", dependencies=["a"]))
    orch.register(AgentDef(name="c", domain="t", dependencies=["a"]))
    orch.register(AgentDef(name="d", domain="t", dependencies=["b", "c"]))
    waves = orch.get_waves()
    assert len(waves) == 3
    assert waves[0] == ["a"]
    assert waves[1] == ["b", "c"]
    assert waves[2] == ["d"]


def test_get_waves_circular_dependency_raises(orch: AgentOrchestrator) -> None:
    """A cycle a<->b cannot be scheduled and raises ValueError listing the stuck set."""
    orch.register(AgentDef(name="a", domain="t", dependencies=["b"]))
    orch.register(AgentDef(name="b", domain="t", dependencies=["a"]))
    with pytest.raises(ValueError, match="Circular dependency"):
        orch.get_waves()


def test_get_waves_self_cycle_raises(orch: AgentOrchestrator) -> None:
    """An agent depending on itself is an unschedulable cycle."""
    orch.register(AgentDef(name="a", domain="t", dependencies=["a"]))
    with pytest.raises(ValueError, match="Circular dependency"):
        orch.get_waves()


def test_get_waves_orphan_dependency_raises(orch: AgentOrchestrator) -> None:
    """A dep on a never-registered agent leaves the dependent permanently unscheduled.

    get_waves treats it as a circular/stuck condition (the orphan dep is never
    in `processed`), so the dependent agent is the stuck set.
    """
    orch.register(AgentDef(name="a", domain="t", dependencies=["ghost"]))
    with pytest.raises(ValueError, match="Circular dependency"):
        orch.get_waves()


# ---------------------------------------------------------------------------
# get_ready — readiness gating
# ---------------------------------------------------------------------------


def test_get_ready_gates_on_dependencies(orch: AgentOrchestrator) -> None:
    """get_ready() returns only agents whose deps are all COMPLETED."""
    orch.register(AgentDef(name="a", domain="t"))
    orch.register(AgentDef(name="b", domain="t", dependencies=["a"]))
    ready = orch.get_ready()
    assert "a" in ready
    assert "b" not in ready

    orch.mark_completed("a")
    ready = orch.get_ready()
    assert "b" in ready
    assert "a" not in ready  # 'a' is no longer PENDING


def test_get_ready_skips_failed_and_running(orch: AgentOrchestrator) -> None:
    """get_ready() ignores agents that are not PENDING (failed/completed)."""
    orch.register(AgentDef(name="a", domain="t"))
    orch.register(AgentDef(name="b", domain="t"))
    orch.mark_failed("a", "boom")
    ready = orch.get_ready()
    assert ready == ["b"]


def test_get_ready_blocked_when_dep_failed(orch: AgentOrchestrator) -> None:
    """A dependent stays not-ready if its dependency FAILED rather than COMPLETED."""
    orch.register(AgentDef(name="a", domain="t"))
    orch.register(AgentDef(name="b", domain="t", dependencies=["a"]))
    orch.mark_failed("a", "dep failed")
    assert "b" not in orch.get_ready()


# ---------------------------------------------------------------------------
# State transitions / failure / retry bookkeeping
# ---------------------------------------------------------------------------


def test_mark_completed(orch: AgentOrchestrator) -> None:
    """mark_completed() flips state to COMPLETED."""
    orch.register(AgentDef(name="a", domain="t"))
    orch.mark_completed("a")
    assert orch.results["a"].state == AgentState.COMPLETED


def test_mark_failed_records_error(orch: AgentOrchestrator) -> None:
    """mark_failed() flips state to FAILED and records the error message."""
    orch.register(AgentDef(name="a", domain="t"))
    orch.mark_failed("a", "Connection timeout")
    assert orch.results["a"].state == AgentState.FAILED
    assert orch.results["a"].error == "Connection timeout"


def test_mark_unknown_agent_is_noop(orch: AgentOrchestrator) -> None:
    """Marking an unregistered agent is a safe no-op (no KeyError)."""
    orch.mark_completed("does-not-exist")
    orch.mark_failed("does-not-exist", "irrelevant")
    assert "does-not-exist" not in orch.results


def test_retry_then_recover_flow(orch: AgentOrchestrator) -> None:
    """A failed agent (with retries budget) can be re-marked completed on retry.

    The orchestrator records retries as policy on AgentDef; a caller that retries
    re-marks the result. We verify the state machine supports FAILED -> COMPLETED.
    """
    orch.register(AgentDef(name="a", domain="t", retries=3))
    orch.mark_failed("a", "transient")
    assert orch.results["a"].state == AgentState.FAILED
    assert orch.agents["a"].retries == 3
    # simulate a successful retry
    orch.mark_completed("a")
    assert orch.results["a"].state == AgentState.COMPLETED


# ---------------------------------------------------------------------------
# validate_all_dependencies
# ---------------------------------------------------------------------------


def test_validate_all_dependencies_passes(orch: AgentOrchestrator) -> None:
    """All deps registered -> validation passes silently."""
    orch.register(AgentDef(name="a", domain="t"))
    orch.register(AgentDef(name="b", domain="t", dependencies=["a"]))
    orch.validate_all_dependencies()  # should not raise


def test_validate_all_dependencies_orphan_raises(orch: AgentOrchestrator) -> None:
    """A dependency on an unregistered agent raises ValueError naming both."""
    orch.register(AgentDef(name="a", domain="t", dependencies=["ghost"]))
    with pytest.raises(ValueError, match="ghost"):
        orch.validate_all_dependencies()


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


def test_summary_structure_and_counts(orch: AgentOrchestrator) -> None:
    """summary() reports total + per-state counts + waves."""
    orch.register(AgentDef(name="a", domain="t"))
    orch.register(AgentDef(name="b", domain="t"))
    orch.register(AgentDef(name="c", domain="t"))
    orch.mark_completed("a")
    orch.mark_failed("b", "err")
    s = orch.summary()
    assert set(s.keys()) == {"total", "completed", "failed", "pending", "waves"}
    assert s["total"] == 3
    assert s["completed"] == 1
    assert s["failed"] == 1
    assert s["pending"] == 1
    assert s["waves"] == [["a", "b", "c"]]


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_reset_clears_state_and_deletes_file(orch: AgentOrchestrator, state_file: Path) -> None:
    """reset() empties agents/results and removes the state file."""
    orch.register(AgentDef(name="a", domain="t"))
    assert state_file.exists()  # register() -> _save() created it
    orch.reset()
    assert orch.agents == {}
    assert orch.results == {}
    assert not state_file.exists()


def test_reset_when_file_absent_is_safe(orch: AgentOrchestrator, state_file: Path) -> None:
    """reset() does not error if the state file was never written / already gone."""
    # No register() call, so no file written yet.
    assert not state_file.exists()
    orch.reset()  # must not raise FileNotFoundError
    assert orch.agents == {}


# ---------------------------------------------------------------------------
# Persistence roundtrip (via the isolated state_file)
# ---------------------------------------------------------------------------


def test_save_writes_state_file(orch: AgentOrchestrator, state_file: Path) -> None:
    """register() persists the agent graph to the JSON state file."""
    orch.register(AgentDef(name="a", domain="t", dependencies=[], retries=4))
    data = json.loads(state_file.read_text())
    assert "a" in data["agents"]
    assert data["agents"]["a"]["domain"] == "t"
    assert data["agents"]["a"]["retries"] == 4
    assert data["agents"]["a"]["state"] == "pending"


def test_reload_restores_graph_and_states(state_file: Path) -> None:
    """A new orchestrator on the same file reloads agents, deps, retries, and states."""
    o1 = AgentOrchestrator(state_file=str(state_file))
    o1.register(AgentDef(name="a", domain="t"))
    o1.register(AgentDef(name="b", domain="t", dependencies=["a"], retries=7))
    o1.mark_completed("a")
    o1.mark_failed("b", "kaput")

    o2 = AgentOrchestrator(state_file=str(state_file))
    assert set(o2.agents.keys()) == {"a", "b"}
    assert o2.agents["b"].dependencies == ["a"]
    assert o2.agents["b"].retries == 7
    assert o2.results["a"].state == AgentState.COMPLETED
    assert o2.results["b"].state == AgentState.FAILED
    assert o2.results["b"].error == "kaput"


# ---------------------------------------------------------------------------
# Fail-open degradation
# ---------------------------------------------------------------------------


def test_load_tolerates_corrupt_state_file(state_file: Path) -> None:
    """A garbage state file must not crash construction (fail-open _load)."""
    state_file.write_text("{ this is not valid json ]")
    orch = AgentOrchestrator(state_file=str(state_file))  # must not raise
    assert orch.agents == {}
    assert orch.results == {}


def test_load_tolerates_malformed_agent_entry(state_file: Path) -> None:
    """A structurally valid JSON missing required keys is swallowed (fail-open)."""
    # 'domain' is required by _load's AgentDef build; its absence triggers the
    # broad except and leaves the orchestrator empty rather than crashing.
    state_file.write_text(json.dumps({"agents": {"a": {"dependencies": []}}}))
    orch = AgentOrchestrator(state_file=str(state_file))
    assert orch.agents == {}


def test_degrades_without_session_manager(monkeypatch, state_file: Path) -> None:
    """When session_manager is unavailable, the orchestrator still works via the file.

    Simulates a fresh install where ``from .session_manager import ...`` failed:
    flip the module-level _HAS_SESSION_MANAGER flag off. _load/_save then rely
    solely on the JSON state file, and the full lifecycle still functions.
    """
    import engine.v16.agent_orchestrator as ao

    monkeypatch.setattr(ao, "_HAS_SESSION_MANAGER", False)

    o = ao.AgentOrchestrator(state_file=str(state_file))
    o.register(ao.AgentDef(name="a", domain="t"))
    o.register(ao.AgentDef(name="b", domain="t", dependencies=["a"]))
    o.mark_completed("a")

    # waves + ready still resolve without any DB backend
    assert o.get_waves() == [["a"], ["b"]]
    assert o.get_ready() == ["b"]

    # state still persisted to the file fallback
    data = json.loads(state_file.read_text())
    assert data["agents"]["a"]["state"] == "completed"

    # and reloadable purely from file, still with session_manager disabled
    o2 = ao.AgentOrchestrator(state_file=str(state_file))
    assert o2.results["a"].state == ao.AgentState.COMPLETED


def test_save_tolerates_db_failure(monkeypatch, state_file: Path) -> None:
    """If the DB save backend raises, _save swallows it and still writes the file.

    The DB write is wrapped in try/except; the /tmp/file write is the durable
    fallback and must still happen.
    """
    import engine.v16.agent_orchestrator as ao

    def boom(*_a, **_k) -> None:
        raise RuntimeError("db down")

    # Ensure the DB path is attempted, then made to fail.
    monkeypatch.setattr(ao, "_HAS_SESSION_MANAGER", True)
    monkeypatch.setattr(ao, "save_v15_state", boom, raising=False)

    o = ao.AgentOrchestrator(state_file=str(state_file))
    o.register(ao.AgentDef(name="a", domain="t"))  # triggers _save -> boom swallowed
    assert state_file.exists()
    data = json.loads(state_file.read_text())
    assert "a" in data["agents"]
