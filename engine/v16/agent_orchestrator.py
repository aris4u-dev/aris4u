import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

# save_v15_state / load_v15_state are always visible to the type checker via
# TYPE_CHECKING so that references inside _HAS_SESSION_MANAGER-guarded blocks
# are never "possibly unbound". At runtime the import may fail (missing module);
# the _HAS_SESSION_MANAGER flag gates every actual call site.
if TYPE_CHECKING:
    from .session_manager import load_v15_state, save_v15_state

_HAS_SESSION_MANAGER = False
try:
    from .session_manager import load_v15_state, save_v15_state  # noqa: F811

    _HAS_SESSION_MANAGER = True
except Exception:
    pass


class AgentState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentDef:
    name: str
    domain: str
    dependencies: list[str] = field(default_factory=list)
    retries: int = 2


@dataclass
class AgentResult:
    agent_name: str
    state: AgentState = AgentState.PENDING
    error: Optional[str] = None


class AgentOrchestrator:
    """Manage parallel agents with dependency constraints."""

    def __init__(self, state_file: str = "/tmp/aris4u_agent_state.json") -> None:
        self.state_file = Path(state_file)
        self.db_key = "agent_orchestrator"
        self.agents: dict[str, AgentDef] = {}
        self.results: dict[str, AgentResult] = {}
        self._load()

    def _load(self) -> None:
        """Load agent state from sessions.db, fall back to /tmp/ JSON.

        Tries sessions.db first for persistence across reboots.
        Falls back to /tmp/ if DB unavailable.
        """
        data = None

        # Try sessions.db first if available
        if _HAS_SESSION_MANAGER:
            try:
                db_data = load_v15_state(self.db_key)
                if db_data:
                    data = db_data
            except Exception:
                pass

        # Fallback to /tmp/ JSON
        if data is None and self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
            except Exception:
                pass

        # Load agents and results from data
        if data:
            try:
                for name, d in data.get("agents", {}).items():
                    self.agents[name] = AgentDef(
                        name=name,
                        domain=d["domain"],
                        dependencies=d.get("dependencies", []),
                        retries=d.get("retries", 2),
                    )
                    self.results[name] = AgentResult(
                        agent_name=name,
                        state=AgentState(d.get("state", "pending")),
                        error=d.get("error"),
                    )
            except Exception:
                pass

    def _save(self) -> None:
        """Save agent state to sessions.db and /tmp/ JSON.

        Tries sessions.db first, always keeps /tmp/ in sync as fallback.
        """
        data = {
            "agents": {
                n: {
                    "domain": a.domain,
                    "dependencies": a.dependencies,
                    "retries": a.retries,
                    "state": self.results[n].state.value if n in self.results else "pending",
                    "error": self.results[n].error if n in self.results else None,
                }
                for n, a in self.agents.items()
            }
        }

        # Save to both DB and /tmp/ (DB primary, /tmp/ hot cache)
        if _HAS_SESSION_MANAGER:
            try:
                save_v15_state(self.db_key, data)
            except Exception:
                pass

        # Keep /tmp/ in sync
        self.state_file.write_text(json.dumps(data))

    def register(self, defn: AgentDef) -> None:
        """Register an agent.

        Args:
            defn: AgentDef to register

        Note:
            Does not validate dependencies at registration time (allows forward refs).
            Call validate_all_dependencies() after all agents are registered.
        """
        self.agents[defn.name] = defn
        self.results[defn.name] = AgentResult(agent_name=defn.name)
        self._save()

    def mark_completed(self, name: str) -> None:
        if name in self.results:
            self.results[name].state = AgentState.COMPLETED
            self._save()

    def mark_failed(self, name: str, error: str) -> None:
        if name in self.results:
            self.results[name].state = AgentState.FAILED
            self.results[name].error = error
            self._save()

    def get_ready(self) -> list[str]:
        ready = []
        for name, defn in self.agents.items():
            if self.results[name].state != AgentState.PENDING:
                continue
            deps_met = all(
                self.results.get(d, AgentResult(d)).state == AgentState.COMPLETED
                for d in defn.dependencies
            )
            if deps_met:
                ready.append(name)
        return ready

    def get_waves(self) -> list[list[str]]:
        """Generate execution waves respecting dependency constraints.

        Returns:
            List of waves, each containing agent names that can run in parallel

        Raises:
            ValueError: If circular dependency detected or orphaned dependencies exist
        """
        waves: list[list[str]] = []
        processed: set[str] = set()
        remaining = set(self.agents.keys())

        while remaining:
            wave = [
                n for n in remaining if all(d in processed for d in self.agents[n].dependencies)
            ]
            if not wave:
                # Circular dependency: agents remain but none can be scheduled
                stuck_agents = sorted(list(remaining))
                raise ValueError(f"Circular dependency detected: {stuck_agents}")
            waves.append(sorted(wave))
            processed.update(wave)
            remaining -= set(wave)

        return waves

    def summary(self) -> dict:
        return {
            "total": len(self.agents),
            "completed": sum(1 for r in self.results.values() if r.state == AgentState.COMPLETED),
            "failed": sum(1 for r in self.results.values() if r.state == AgentState.FAILED),
            "pending": sum(1 for r in self.results.values() if r.state == AgentState.PENDING),
            "waves": self.get_waves(),
        }

    def validate_all_dependencies(self) -> None:
        """Validate that all dependencies refer to registered agents.

        Call this after all agents are registered.

        Raises:
            ValueError: If agent depends on non-existent agent
        """
        for name, defn in self.agents.items():
            for dep in defn.dependencies:
                if dep not in self.agents:
                    raise ValueError(f"Agent '{name}' depends on '{dep}' which is not registered")

    def reset(self) -> None:
        self.agents.clear()
        self.results.clear()
        if self.state_file.exists():
            self.state_file.unlink()
