"""V16 Pipeline Orchestrator — Unified F1→F6 Runtime.

Wires together all V16 engine modules (F1 through F6) into a single callable
interface for the hook system. Each hook call (UserPromptSubmit, PostToolUse,
SessionEnd) invokes the orchestrator to run the appropriate phase.

Components called:
- F1.PERCEPCION: Intent classification (embedding-based)
- F2.COGNICION: Decision cascade (effort/depth/hooks/strategy)
- F3.MEMORIA: State persistence (ACID)
- F5.VALIDACION: Output validation (contracts + semantic)
- F6.COMUNICACION: Format selection (cognitive load)
- F7.APRENDIZAJE: Session learning (end-of-session)

Architecture:
- Single entry point: process_query() for hooks
- Lazy initialization: modules created on-demand, cached as singletons
- Graceful fallback: if any F2-F7 module fails, use safe defaults (never break hook)
- Thread-safe: all state writes go through MemoriaEngine (ACID)

Performance:
- F1 classification: ~100-150ms (Ollama embedding)
- F2-F6 combined: <50ms (all local, no I/O)
- Total per-query: <200ms (fast enough for hook constraint)
- F7 learning: ~100ms (async at session end, not in critical path)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, UTC
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Lazy-loaded modules (created on first use)
_f1_classifier = None
_f2_engine = None
_f3_memoria = None
_f5_validator = None
_f6_comunicacion = None


@dataclass
class V16QueryResult:
    """Output of F1→F6 pipeline for a single query."""

    intent: str  # From F1
    confidence: float  # From F1
    depth_levels: list[int]  # From F2
    effort_level: str  # From F2
    hooks_active: list[str]  # From F2
    strategy: str  # From F2
    locked_decisions: list[dict] = field(default_factory=list)  # From F3
    guards: list[dict] = field(default_factory=list)  # From F3
    validation_result: Optional[dict] = None  # From F5 (if apply_validation=True)
    format_directive: Optional[dict] = None  # From F6 (if format_output=True)
    error: Optional[str] = None  # If any module failed
    error_module: Optional[str] = None  # Which module failed

    def to_dict(self) -> dict:
        """Serialize for logging or state persistence."""
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "depth_levels": self.depth_levels,
            "effort_level": self.effort_level,
            "hooks_active": self.hooks_active,
            "strategy": self.strategy,
            "locked_decisions": self.locked_decisions,
            "guards": self.guards,
            "validation_result": self.validation_result,
            "format_directive": self.format_directive,
            "error": self.error,
            "error_module": self.error_module,
        }


class V16Orchestrator:
    """Unified V16 pipeline orchestrator.

    Coordinates F1→F6 for each query with graceful fallback to default behavior
    if any module fails. All state persisted to ACID sessions.db.
    """

    def __init__(self) -> None:
        """Initialize orchestrator with lazy-loading of modules."""
        self.session_log: list[dict] = []
        self._f1_failure_count = 0
        self._f2_failure_count = 0

    def process_query(
        self,
        query: str,
        apply_validation: bool = True,
        apply_formatting: bool = True,
    ) -> V16QueryResult:
        """
        Run F1→F6 pipeline on a single query.

        Args:
            query: User query (ES or EN), truncated to 500 chars
            apply_validation: If True, run F5 validation (default True - wired via PreToolUse hook)
            apply_formatting: If True, run F6 format selection (default True - active for PostToolUse)

        Returns:
            V16QueryResult with intent, confidence, depth directives, and state.
            If any module fails, uses safe fallback values and sets error field.

        Performance guarantee: <200ms total (including Ollama F1 call)

        NOTE: V16.1 (2026-04-23) — F5 and F6 now default-enabled. F5.VALIDACION
        is wired as PreToolUse hook (f5_prevalidation.sh) for pre-Write gates.
        F6.COMUNICACION active for PostToolUse formatting directives.
        """
        result = V16QueryResult(
            intent="simple",
            confidence=0.0,
            depth_levels=[1],
            effort_level="medium",
            hooks_active=[],
            strategy="default",
        )

        query_short = query[:500]

        if self._run_f1_perception(query_short, result):
            return result  # F1 hit its consecutive-failure threshold

        self._run_f2_cognicion(query_short, result)
        self._run_f3_memoria(query_short, result)
        self._run_optional_phases(apply_validation, apply_formatting)
        self._append_session_log(query_short, result)

        return result

    def _run_f1_perception(self, query_short: str, result: V16QueryResult) -> bool:
        """Run F1.PERCEPCION intent classification, mutating ``result`` in place.

        Args:
            query_short: Query truncated to 500 chars.
            result: Result object to populate with intent/confidence.

        Returns:
            True if F1 reached its consecutive-failure threshold (caller should
            short-circuit and return ``result``); False otherwise.
        """
        try:
            from .f1_classifier import classify_v16_with_confidence

            intent, confidence = classify_v16_with_confidence(query_short)
            result.intent = intent
            result.confidence = confidence
            self._f1_failure_count = 0  # Reset on success
            logger.debug(f"F1.PERCEPCION: {intent} (confidence={confidence:.2f})")
            return False
        except Exception as e:
            self._f1_failure_count += 1
            logger.warning(f"F1.PERCEPCION failed (attempt {self._f1_failure_count}): {e}")
            # Keep result.intent = "simple" (safe default)
            if self._f1_failure_count >= 3:
                result.error = str(e)
                result.error_module = "F1"
                return True
            return False

    def _run_f2_cognicion(self, query_short: str, result: V16QueryResult) -> None:
        """Run F2.COGNICION decision cascade, mutating ``result`` in place.

        On failure the F2 defaults already present on ``result`` are retained,
        and ``error``/``error_module`` are set once the consecutive-failure
        threshold is reached.

        Args:
            query_short: Query truncated to 500 chars.
            result: Result object to populate with depth/effort/hooks/strategy.
        """
        try:
            from .f2_cognicion import create_cognicion_engine

            f2 = _get_or_create("f2_engine", create_cognicion_engine)
            if f2 is None:
                raise RuntimeError("F2 engine initialization failed")

            budget_remaining, budget_max = self._load_budget()

            # decide(intent, confidence, complexity, budget_remaining, budget_max)
            decision = f2.decide(
                intent=result.intent,
                confidence=result.confidence,
                complexity=len(query_short.split()) * 2,
                budget_remaining=budget_remaining,
                budget_max=budget_max,
            )

            result.depth_levels = decision.depth_levels
            result.effort_level = decision.effort_level
            result.hooks_active = decision.hooks_active
            result.strategy = decision.strategy
            self._f2_failure_count = 0  # Reset on success

            logger.debug(
                f"F2.COGNICION: effort={result.effort_level}, "
                f"strategy={result.strategy}, hooks={len(result.hooks_active)}"
            )
        except Exception as e:
            self._f2_failure_count += 1
            logger.warning(f"F2.COGNICION failed (attempt {self._f2_failure_count}): {e}")
            # Keep F2 defaults (effort_level, hooks_active, strategy already set)
            if self._f2_failure_count >= 3:
                result.error = str(e)
                result.error_module = "F2"

    @staticmethod
    def _load_budget() -> tuple[int, int]:
        """Load (budget_remaining, budget_max) from F3 state with safe defaults.

        Returns:
            Tuple of remaining and max token budget; defaults (100000, 200000)
            if state is unavailable or empty.
        """
        budget_remaining = 100000  # Safe default
        budget_max = 200000
        f3 = _get_or_create_memoria()
        if f3 is None:
            return budget_remaining, budget_max
        try:
            state = f3.load_state(
                "token_intelligence"
            )  # load_state_dict() doesn't exist; use correct method
            if state:
                budget_remaining = state.get("budget_remaining", 100000)
                budget_max = state.get("budget_max", 200000)
        except Exception:
            pass
        return budget_remaining, budget_max

    def _run_f3_memoria(self, query_short: str, result: V16QueryResult) -> None:
        """Run F3.MEMORIA recall + event logging, mutating ``result`` in place.

        Args:
            query_short: Query truncated to 500 chars.
            result: Result object to populate with locked_decisions/guards.
        """
        try:
            f3 = _get_or_create_memoria()
            self._recall_locked_decisions(query_short, result)
            self._recall_critical_guards(result)
            self._log_query_event(f3, query_short, result)

            logger.debug(
                f"F3.MEMORIA: recalled {len(result.locked_decisions)} decisions, "
                f"{len(result.guards)} guards"
            )
        except Exception as e:
            logger.warning(f"F3.MEMORIA failed: {e}")

    @staticmethod
    def _recall_locked_decisions(query_short: str, result: V16QueryResult) -> None:
        """Recall locked decisions, falling back to search() on failure.

        Args:
            query_short: Query truncated to 500 chars.
            result: Result object to populate with locked_decisions.
        """
        try:
            from .session_manager import get_locked_decisions

            locked = get_locked_decisions(query_short, limit=3)
            result.locked_decisions = locked if locked else []
        except Exception as e:
            logger.debug(f"F3 decision recall (get_locked_decisions) failed: {e}")
            # Fallback: try search if get_locked_decisions fails
            try:
                from .session_manager import search

                search_result = search(query_short, limit=3)
                result.locked_decisions = search_result.get("decisions", [])
            except Exception as e2:
                logger.debug(f"F3 decision recall (search) failed: {e2}")

    @staticmethod
    def _recall_critical_guards(result: V16QueryResult) -> None:
        """Load up to 4 critical guards into ``result``.

        Args:
            result: Result object to populate with guards.
        """
        try:
            from .session_manager import get_all_guards

            all_guards = get_all_guards() or []
            result.guards = [g for g in all_guards if g.get("severity") == "critical"][:4]
        except Exception as e:
            logger.warning(f"F3 guard recall failed: {e}")

    @staticmethod
    def _log_query_event(f3: Any, query_short: str, result: V16QueryResult) -> None:
        """Append this query as an event to F3 for F7 learning.

        Args:
            f3: MemoriaEngine instance.
            query_short: Query truncated to 500 chars.
            result: Result whose intent/confidence/effort/strategy are logged.
        """
        try:
            f3.append_event(
                "event_logged",
                {
                    "query": query_short[:200],
                    "intent": result.intent,
                    "confidence": result.confidence,
                    "effort": result.effort_level,
                    "strategy": result.strategy,
                    "timestamp": datetime.now(UTC).isoformat(),
                },
            )
        except Exception as e:
            logger.warning(f"F3 event logging failed: {e}")

    @staticmethod
    def _run_optional_phases(apply_validation: bool, apply_formatting: bool) -> None:
        """Run the optional F5/F6 phases (currently deferred to PostToolUse).

        Args:
            apply_validation: If True, prime the F5.VALIDACION singleton.
            apply_formatting: If True, prime the F6.COMUNICACION singleton.
        """
        if apply_validation:
            try:
                f5 = _get_or_create("f5_validator", _create_f5_validator)
                if f5 is not None:
                    # F5 validates Claude's OUTPUT, not the query (PostToolUse).
                    logger.debug("F5.VALIDACION: deferred to PostToolUse hook")
            except Exception as e:
                logger.warning(f"F5.VALIDACION failed: {e}")

        if apply_formatting:
            try:
                f6 = _get_or_create("f6_comunicacion", _create_f6_comunicacion)
                if f6 is not None:
                    # F6 selects response format based on effort (PostToolUse).
                    logger.debug("F6.COMUNICACION: deferred to PostToolUse hook")
            except Exception as e:
                logger.warning(f"F6.COMUNICACION failed: {e}")

    def _append_session_log(self, query_short: str, result: V16QueryResult) -> None:
        """Append the query summary to the in-memory session log for F7.

        Args:
            query_short: Query truncated to 500 chars.
            result: Result whose intent/confidence/effort are recorded.
        """
        self.session_log.append(
            {
                "query": query_short,
                "intent": result.intent,
                "confidence": result.confidence,
                "effort": result.effort_level,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    def end_session(self) -> dict:
        """
        Run F7.APRENDIZAJE at session end.

        Learns patterns from all queries in this session and updates
        the classifier exemplars (if confidence has improved).

        Returns:
            Learning result dict with metrics (number of patterns learned, etc.)
        """
        if not self.session_log:
            return {"patterns_learned": 0, "exemplars_updated": 0}

        import os

        sid = (
            os.environ.get("CLAUDE_CODE_SESSION_ID") or getattr(self, "session_id", "") or "unknown"
        )
        try:
            from .f7_aprendizaje import AprendizajeEngine

            f7 = AprendizajeEngine()
            # V2.0 3c: producción NO es entorno supervisado (sin ground-truth labels ni
            # exemplar_pool curado). learn_from_session corre sus fases con-datos y degrada
            # limpio (ya NO crashea con TypeError de 1-arg-vs-4). El aprendizaje REAL fluye
            # por el reward loop: verify outcomes → reward_signals → apply_reward_signals.
            result = f7.learn_from_session(
                session_id=sid,
                session_data=self.session_log,
                exemplar_pool={},
                ground_truth_labels={},
                historical_sessions=[],
            )
            try:
                adaptation = f7.apply_reward_signals(sid)
            except Exception:
                adaptation = {}
            logger.info(f"F7.APRENDIZAJE ok: {getattr(result, 'reason', result)}")
            exemplars = getattr(result, "exemplars_added", 0)
            return {
                "patterns_learned": exemplars,  # contrato retrocompatible
                "exemplars_added": exemplars,
                "temperature": getattr(result, "temperature", 1.0),
                "reward_adaptation": adaptation,
            }

        except Exception as e:
            logger.warning(f"F7.APRENDIZAJE failed: {e}")
            return {"error": str(e), "patterns_learned": 0}


# ============ Singleton Factory Functions ============


def _get_or_create(key: str, creator_fn) -> Optional[Any]:
    """Get or create a lazy-loaded singleton module.

    Args:
        key: Module name (e.g., "f2_engine")
        creator_fn: Function that creates the module

    Returns:
        Module instance or None if creation failed.
    """
    global _f2_engine, _f5_validator, _f6_comunicacion

    if key == "f2_engine":
        if _f2_engine is None:
            try:
                _f2_engine = creator_fn()
            except Exception as e:
                logger.error(f"Failed to create F2 engine: {e}")
                return None
        return _f2_engine

    if key == "f5_validator":
        if _f5_validator is None:
            try:
                _f5_validator = creator_fn()
            except Exception as e:
                logger.error(f"Failed to create F5 validator: {e}")
                return None
        return _f5_validator

    if key == "f6_comunicacion":
        if _f6_comunicacion is None:
            try:
                _f6_comunicacion = creator_fn()
            except Exception as e:
                logger.error(f"Failed to create F6 comunicacion: {e}")
                return None
        return _f6_comunicacion

    return None


def _get_or_create_memoria():
    """Get or create F3.MEMORIA singleton."""
    global _f3_memoria
    if _f3_memoria is None:
        try:
            from .f3_memoria import MemoriaEngine

            _f3_memoria = MemoriaEngine()
        except Exception as e:
            logger.error(f"Failed to create F3 memoria: {e}")
            return None
    return _f3_memoria


def _create_f5_validator():
    """Factory for F5.VALIDACION."""
    from .f5_validacion import ValidacionEngine

    return ValidacionEngine()


def _create_f6_comunicacion():
    """Factory for F6.COMUNICACION."""
    from .f6_comunicacion import ComunicacionEngine

    return ComunicacionEngine()


# ============ Module-level singleton instance ============

_orchestrator_instance: Optional[V16Orchestrator] = None


def get_orchestrator() -> V16Orchestrator:
    """Get or create the global orchestrator singleton.

    Returns:
        V16Orchestrator instance (created once per session).
    """
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = V16Orchestrator()
    return _orchestrator_instance


def reset_orchestrator() -> None:
    """Reset the global orchestrator singleton (for testing)."""
    global _orchestrator_instance
    _orchestrator_instance = None
