from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .config import (
    EFFORT_LEVEL_MAPPING,
    TERSE_THRESHOLD_PCT,
    TOKEN_BUDGET_MAX_TOKENS,
    TOKEN_ESTIMATE_PROMPT_PREFIX,
    TOKEN_ESTIMATE_RATIO,
)

# MemoriaEngine is always visible to the type checker via TYPE_CHECKING so that
# references below are never "possibly unbound". At runtime the import may fail
# (e.g. sqlite3 missing or circular import during early boot); the _HAS_MEMORIA
# flag gates every actual call site.
if TYPE_CHECKING:
    from .f3_memoria import MemoriaEngine

_HAS_MEMORIA = False
try:
    from .f3_memoria import MemoriaEngine  # noqa: F811

    _HAS_MEMORIA = True
except Exception:
    pass


class TokenIntelligence:
    """Track token usage, estimate budget, route effort levels.

    V16: Uses MemoriaEngine for ACID-compliant persistence instead of /tmp JSON.
    """

    def __init__(self) -> None:
        """Initialize TokenIntelligence. State persists via MemoriaEngine (ACID)."""
        self.db_key = "token_intelligence"
        self.state = self._load()
        self.memoria = None
        if _HAS_MEMORIA:
            try:
                self.memoria = MemoriaEngine()
            except Exception:
                pass

    def _load(self) -> dict:
        """Load state from MemoriaEngine (ACID-backed).

        Returns:
            State dictionary with token tracking info
        """
        if _HAS_MEMORIA:
            try:
                engine = MemoriaEngine()
                state = engine.load_state(self.db_key)
                if state:
                    return state
            except Exception:
                pass

        return {}

    def _save(self) -> None:
        """Save state to MemoriaEngine (ACID transaction).

        Uses WAL mode for durability and concurrent safety.
        """
        if self.memoria:
            try:
                self.memoria.save_state(self.db_key, self.state)
            except Exception:
                pass

    def estimate_tokens(self, text: str, category: str = "prompt") -> int:
        chars = len(text)
        base = chars // TOKEN_ESTIMATE_RATIO
        if category == "prompt":
            return base + TOKEN_ESTIMATE_PROMPT_PREFIX
        if category == "tool_call":
            return (chars // 5) + 200
        return base

    def log_query(self, query_text: str, query_type: str) -> None:
        estimated = self.estimate_tokens(query_text, "prompt")
        self.state.setdefault("accumulated_token_estimate", 0)
        self.state["accumulated_token_estimate"] += estimated
        log = self.state.setdefault("token_log", [])
        log.append(
            {
                "ts": datetime.now().isoformat(),
                "type": query_type,
                "est": estimated,
                "cum": self.state["accumulated_token_estimate"],
            }
        )
        if len(log) > 100:
            self.state["token_log"] = log[-50:]
        self._save()

    def get_budget_remaining(self) -> int:
        acc = self.state.get("accumulated_token_estimate", 0)
        return max(0, TOKEN_BUDGET_MAX_TOKENS - acc)

    def get_budget_pct(self) -> float:
        acc = self.state.get("accumulated_token_estimate", 0)
        return (acc / TOKEN_BUDGET_MAX_TOKENS) * 100

    def get_effort_level(self, query_type: str) -> str:
        pct = self.get_budget_pct()
        if pct > 80:
            return "low"
        if pct > 60:
            downgrade = {
                "simple": "low",
                "fix": "low",
                "decision": "medium",
                "implementation": "high",
            }
            return downgrade.get(query_type, "medium")
        return EFFORT_LEVEL_MAPPING.get(query_type, "medium")

    def get_terse_directive(self) -> Optional[str]:
        pct = self.get_budget_pct()
        if pct > 75:
            return "OUTPUT: Extreme terse. Bullet points only. No prose. Abbreviate."
        if pct > TERSE_THRESHOLD_PCT:
            return (
                "OUTPUT: Terse. Minimal sentences. No markdown headers. Code inline if <30 chars."
            )
        return None

    def reset_budget(self) -> None:
        """Reset accumulated token estimate and clear token log.

        Called at wave boundaries when a new session phase starts.
        Allows effort level to re-escalate if budget recovers.
        """
        self.state["accumulated_token_estimate"] = 0
        self.state["token_log"] = []
        self._save()

    def get_budget_health(self) -> dict:
        """Get detailed budget health status.

        Returns:
            Dict with status, pct_used, estimated_remaining, and can_reset flag
        """
        pct = self.get_budget_pct()
        remaining = self.get_budget_remaining()

        if pct > 80:
            status = "critical"
        elif pct > 60:
            status = "warning"
        else:
            status = "healthy"

        return {
            "status": status,
            "pct_used": round(pct, 1),
            "estimated_remaining": remaining,
            "can_reset": True,  # reset_budget() can always be called at wave boundaries
        }

    def session_summary(self) -> dict:
        return {
            "total_estimated": self.state.get("accumulated_token_estimate", 0),
            "budget_max": TOKEN_BUDGET_MAX_TOKENS,
            "budget_pct": round(self.get_budget_pct(), 1),
            "queries_logged": len(self.state.get("token_log", [])),
        }
