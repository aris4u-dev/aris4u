"""ARIS4U V16 Engine — Core orchestration and assessment framework.

Public API exports from all V16 subsystems.

NOTE (perf): f8_assessment is NOT imported here. That module (1440 LOC) pulls
httpx + rich (~67ms) and belongs to the pentest vertical deferred to Tramo 4.
No hooks/dispatch caller uses its symbols at the package level — its own tests
import directly from engine.v16.f8_assessment. Loading it eagerly would cost
~67ms on every UserPromptSubmit/SessionStart/SessionEnd hook invocation.
"""

# V16.10 H44: Soft Reward Q-loop exports
from . import soft_reward_loop

__all__ = [
    "soft_reward_loop",
]
