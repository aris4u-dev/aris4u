"""F6.COMUNICACION — Token Counting, Format Adaptation, Compression, Progress Reporting.

Actualizado: 2026-04-23
V16 Engine: Replace broken chars/4 token estimation with Anthropic API token counting.
"""

import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx

from .config import CLAUDE_MODEL

logger = logging.getLogger(__name__)

# DB anclada al repo (NO relativa al cwd): si el default fuera "data/sessions.db" relativo,
# instanciar estas clases con otro cwd (p.ej. un agente trabajando en console/) crearía una
# sessions.db basura ahí. parents[2] = raíz del repo aris4u (engine/v16/ → engine/ → aris4u/).
_DEFAULT_DB = str(Path(__file__).resolve().parents[2] / "data" / "sessions.db")


# ============================================================================
# COMPONENT 1: TOKEN COUNTING (G1) — ANTHROPIC API + FALLBACK
# ============================================================================


@dataclass
class TokenCountRequest:
    """Request object for token counting."""

    model: str
    system_prompt: str
    messages: list[dict]
    tools: Optional[list[dict]] = None


@dataclass
class TokenCountResult:
    """Result object with exact token counts."""

    input_tokens: int
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    total_tokens: int = 0
    source: str = "api"  # "api", "cache", "tiktoken_fallback", "chars_fallback"
    query_hash: str = ""


class TokenCounter:
    """Count tokens using Anthropic API with local fallback.

    Free token counting API: https://api.anthropic.com/v1/messages/count_tokens
    Rate: Unlimited, <10ms latency, 99% accurate.
    Fallback: tiktoken cl100k_base (~90% accurate), then chars/4.
    """

    def __init__(self, api_key: Optional[str] = None, db_path: str = _DEFAULT_DB) -> None:
        """Initialize TokenCounter.

        Args:
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
            db_path: Path to SQLite cache database
        """
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.db_path = db_path
        self.logger = logging.getLogger(__name__)
        self._init_cache_table()
        self._tiktoken_available = self._check_tiktoken()

    def _check_tiktoken(self) -> bool:
        """Check if tiktoken is available for fallback."""
        try:
            import tiktoken  # noqa: F401
            return True
        except ImportError:
            return False

    def _init_cache_table(self) -> None:
        """Initialize token_counts cache table in database."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS token_counts (
                    query_hash TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    cache_creation_tokens INTEGER DEFAULT 0,
                    cache_read_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(query_hash, model)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_token_counts_model "
                "ON token_counts(model, timestamp DESC)"
            )
            conn.commit()
            conn.close()
        except Exception as e:
            self.logger.warning(f"Failed to initialize token cache table: {e}")

    def _make_query_hash(self, req: TokenCountRequest) -> str:
        """Hash system + first message to create cache key.

        Avoids collision by hashing only system + first 256 chars of first message.
        """
        key = f"{req.system_prompt}|{req.messages[0]['content'][:256]}" if req.messages else req.system_prompt
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _get_cached_count(self, req: TokenCountRequest) -> Optional[TokenCountResult]:
        """Check if token count is cached.

        Args:
            req: TokenCountRequest

        Returns:
            TokenCountResult if cached, else None
        """
        query_hash = self._make_query_hash(req)
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.execute(
                "SELECT input_tokens, cache_creation_tokens, cache_read_tokens, total_tokens "
                "FROM token_counts WHERE query_hash = ? AND model = ?",
                (query_hash, req.model),
            )
            row = cursor.fetchone()
            conn.close()

            if row:
                return TokenCountResult(
                    input_tokens=row[0],
                    cache_creation_tokens=row[1],
                    cache_read_tokens=row[2],
                    total_tokens=row[3],
                    source="cache",
                    query_hash=query_hash,
                )
        except Exception as e:
            self.logger.warning(f"Cache lookup failed: {e}")

        return None

    def _save_to_cache(self, result: TokenCountResult, model: str) -> None:
        """Save token count result to cache.

        Args:
            result: TokenCountResult to cache
            model: Model name
        """
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                INSERT OR REPLACE INTO token_counts
                (query_hash, model, input_tokens, cache_creation_tokens,
                 cache_read_tokens, total_tokens)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.query_hash,
                    model,
                    result.input_tokens,
                    result.cache_creation_tokens,
                    result.cache_read_tokens,
                    result.total_tokens,
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            self.logger.warning(f"Cache save failed: {e}")

    async def count_tokens_async(self, req: TokenCountRequest) -> TokenCountResult:
        """Count tokens using Anthropic API (async).

        Args:
            req: TokenCountRequest

        Returns:
            TokenCountResult with exact token counts

        Cost: Free (Anthropic doesn't bill for count_tokens)
        Latency: 5-15ms typical
        """
        # Try cache first
        cached = self._get_cached_count(req)
        if cached:
            self.logger.debug(f"Cache hit: {cached.query_hash} = {cached.total_tokens} tokens")
            return cached

        # Call Anthropic API
        if not self.api_key:
            self.logger.warning("No ANTHROPIC_API_KEY set, using fallback estimation")
            return self._fallback_estimate(req)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages/count_tokens",
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": req.model,
                        "system": req.system_prompt,
                        "messages": req.messages,
                        "tools": req.tools or [],
                    },
                    timeout=5.0,
                )

                if response.status_code != 200:
                    self.logger.error(f"API error: {response.status_code} {response.text}")
                    return self._fallback_estimate(req)

                data = response.json()
                result = TokenCountResult(
                    input_tokens=data.get("input_tokens", 0),
                    cache_creation_tokens=data.get("cache_creation_input_tokens", 0),
                    cache_read_tokens=data.get("cache_read_input_tokens", 0),
                    source="api",
                    query_hash=self._make_query_hash(req),
                )
                result.total_tokens = (
                    result.input_tokens + result.cache_creation_tokens + result.cache_read_tokens
                )

                self.logger.info(f"API call: {req.model} = {result.total_tokens} tokens")

                # Cache result
                self._save_to_cache(result, req.model)
                return result

        except Exception as e:
            self.logger.error(f"Token counting API failed: {e}")
            return self._fallback_estimate(req)

    def count_tokens(self, req: TokenCountRequest) -> TokenCountResult:
        """Count tokens synchronously (blocking).

        Args:
            req: TokenCountRequest

        Returns:
            TokenCountResult with exact token counts
        """
        # Try cache first
        cached = self._get_cached_count(req)
        if cached:
            self.logger.debug(f"Cache hit: {cached.query_hash} = {cached.total_tokens} tokens")
            return cached

        # Fallback immediately if no API key
        if not self.api_key:
            self.logger.warning("No ANTHROPIC_API_KEY set, using fallback estimation")
            return self._fallback_estimate(req)

        # Try sync HTTP call
        try:
            response = httpx.post(
                "https://api.anthropic.com/v1/messages/count_tokens",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": req.model,
                    "system": req.system_prompt,
                    "messages": req.messages,
                    "tools": req.tools or [],
                },
                timeout=5.0,
            )

            if response.status_code != 200:
                self.logger.error(f"API error: {response.status_code} {response.text}")
                return self._fallback_estimate(req)

            data = response.json()
            result = TokenCountResult(
                input_tokens=data.get("input_tokens", 0),
                cache_creation_tokens=data.get("cache_creation_input_tokens", 0),
                cache_read_tokens=data.get("cache_read_input_tokens", 0),
                source="api",
                query_hash=self._make_query_hash(req),
            )
            result.total_tokens = (
                result.input_tokens + result.cache_creation_tokens + result.cache_read_tokens
            )

            self.logger.info(f"API call: {req.model} = {result.total_tokens} tokens")

            # Cache result
            self._save_to_cache(result, req.model)
            return result

        except Exception as e:
            self.logger.error(f"Token counting API failed: {e}")
            return self._fallback_estimate(req)

    def _fallback_estimate(self, req: TokenCountRequest) -> TokenCountResult:
        """Fallback token estimation using tiktoken or chars/4.

        Args:
            req: TokenCountRequest

        Returns:
            TokenCountResult with estimated tokens
        """
        query_hash = self._make_query_hash(req)

        # Try tiktoken first (90% accurate, ~1ms)
        if self._tiktoken_available:
            try:
                import tiktoken

                enc = tiktoken.get_encoding("cl100k_base")

                total_text = req.system_prompt + "\n"
                for msg in req.messages:
                    total_text += msg.get("content", "") + "\n"

                estimated_tokens = len(enc.encode(total_text))

                self.logger.warning(
                    f"Using tiktoken fallback: {estimated_tokens} tokens "
                    f"(±10% accuracy, API unavailable)"
                )

                return TokenCountResult(
                    input_tokens=estimated_tokens,
                    source="tiktoken_fallback",
                    query_hash=query_hash,
                    total_tokens=estimated_tokens,
                )
            except Exception as e:
                self.logger.error(f"Tiktoken fallback failed: {e}")

        # Last resort: chars / 4 (very rough, ±50%)
        self.logger.error("Both API and tiktoken failed. Using chars/4 estimation (inaccurate).")
        estimated = sum(len(m.get("content", "")) for m in req.messages) // 4
        estimated = max(estimated, 1)  # At least 1 token

        return TokenCountResult(
            input_tokens=estimated,
            source="chars_fallback",
            query_hash=query_hash,
            total_tokens=estimated,
        )


def count_tokens_simple(system: str, messages: list[dict], model: str = CLAUDE_MODEL) -> int:
    """Convenience function. Returns exact token count.

    Args:
        system: System prompt
        messages: List of message dicts with 'role' and 'content'
        model: Claude model name

    Returns:
        Total token count (int)
    """
    counter = TokenCounter()
    req = TokenCountRequest(model=model, system_prompt=system, messages=messages)
    return counter.count_tokens(req).total_tokens


# ============================================================================
# COMPONENT 3: FORMAT ADAPTATION (G3)
# ============================================================================


class EffortLevel(str, Enum):
    """Query effort level."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


class OutputFormat(str, Enum):
    """Output format types."""

    BULLET_LIST = "bullets"
    STRUCTURED = "structured"
    NARRATIVE = "narrative"
    DETAILED = "detailed"


@dataclass
class FormatDirective:
    """Format settings for output."""

    format_type: OutputFormat
    max_paragraphs: int
    include_code: bool
    include_table: bool
    include_examples: bool
    include_references: bool
    max_items_per_list: int
    progressive_disclosure: bool


class FormatSelector:
    """Select output format based on effort level."""

    FORMAT_MATRIX = {
        EffortLevel.LOW: FormatDirective(
            format_type=OutputFormat.BULLET_LIST,
            max_paragraphs=1,
            include_code=False,
            include_table=False,
            include_examples=False,
            include_references=False,
            max_items_per_list=7,
            progressive_disclosure=False,
        ),
        EffortLevel.MEDIUM: FormatDirective(
            format_type=OutputFormat.STRUCTURED,
            max_paragraphs=3,
            include_code=True,
            include_table=False,
            include_examples=True,
            include_references=False,
            max_items_per_list=7,
            progressive_disclosure=True,
        ),
        EffortLevel.HIGH: FormatDirective(
            format_type=OutputFormat.NARRATIVE,
            max_paragraphs=5,
            include_code=True,
            include_table=True,
            include_examples=True,
            include_references=True,
            max_items_per_list=7,
            progressive_disclosure=True,
        ),
        EffortLevel.XHIGH: FormatDirective(
            format_type=OutputFormat.DETAILED,
            max_paragraphs=999,
            include_code=True,
            include_table=True,
            include_examples=True,
            include_references=True,
            max_items_per_list=7,
            progressive_disclosure=False,
        ),
    }

    def select_format(self, effort_level: EffortLevel, output_tokens: int = 0) -> FormatDirective:
        """Select format based on effort level.

        Args:
            effort_level: EffortLevel enum
            output_tokens: Optional token count for progressive disclosure

        Returns:
            FormatDirective with template settings
        """
        return self.FORMAT_MATRIX[effort_level]


class FormatApplier:
    """Apply formatting directives to raw output."""

    def apply_format(self, raw_output: str, directive: FormatDirective) -> str:
        """Apply format directive to output.

        Args:
            raw_output: Unformatted text
            directive: FormatDirective with settings

        Returns:
            Formatted output
        """
        return self._apply_cognitive_load_constraints(raw_output, directive)

    def _apply_cognitive_load_constraints(self, text: str, directive: FormatDirective) -> str:
        """Apply Miller 7±2 rule and paragraph limits.

        Args:
            text: Input text
            directive: FormatDirective with constraints

        Returns:
            Constrained text
        """
        lines = text.split("\n")

        # Limit list items to 7 per section
        result = []
        list_count = 0
        in_list = False

        for i, line in enumerate(lines):
            is_list_item = line.strip().startswith(("-", "•", "*")) or re.match(r"^\d+\.\s", line)

            if is_list_item:
                list_count += 1
                if list_count <= directive.max_items_per_list:
                    result.append(line)
                elif list_count == directive.max_items_per_list + 1:
                    remaining = len([ln for ln in lines[i:] if ln.strip().startswith(("-", "•", "*"))])
                    result.append(f"*(and {remaining} more items)*")
                in_list = True
            else:
                if in_list and line.strip() == "":
                    list_count = 0
                    in_list = False
                result.append(line)

        # Limit paragraphs
        para_count = len([ln for ln in result if ln.strip() == ""])
        if para_count > directive.max_paragraphs:
            # Estimate where to truncate
            result_truncated = result[: int(len(result) * directive.max_paragraphs / max(1, para_count))]
            result_truncated.append("\n*(Full version available on request)*")
            result = result_truncated

        return "\n".join(result)


def format_output(output: str, effort_level: EffortLevel, output_tokens: int = 0) -> str:
    """Convenience function to format output.

    Args:
        output: Raw output text
        effort_level: EffortLevel enum
        output_tokens: Optional token count

    Returns:
        Formatted output
    """
    selector = FormatSelector()
    applier = FormatApplier()

    directive = selector.select_format(effort_level, output_tokens)
    return applier.apply_format(output, directive)


# ============================================================================
# COMPONENT 5: COGNITIVE LOAD ENFORCER (G5) — MILLER 7±2 RULE
# ============================================================================


@dataclass
class CognitiveLoadAnalysis:
    """Result of cognitive load analysis."""

    total_items: int
    items_per_section: dict
    violations: list[str]
    is_overloaded: bool
    recommendations: list[str]


class CognitiveLoadEnforcer:
    """Enforce Miller's 7±2 rule: max 7 items per list/section."""

    MILLER_LIMIT = 7

    def analyze(self, text: str) -> CognitiveLoadAnalysis:
        """Analyze cognitive load of output.

        Args:
            text: Output text

        Returns:
            CognitiveLoadAnalysis
        """
        lines = text.split("\n")

        items_by_section = {}
        current_section = "root"
        item_count = 0
        violations = []

        for line in lines:
            # Detect section headers
            if line.startswith("#"):
                current_section = line.lstrip("#").strip()
                if item_count > self.MILLER_LIMIT:
                    violations.append(f"{current_section or 'root'}: {item_count} items")
                items_by_section[current_section] = 0
                item_count = 0
                continue

            # Count list items
            if (
                line.strip().startswith(("-", "*", "•"))
                or re.match(r"^\d+\.\s", line)
            ):
                item_count += 1
                items_by_section[current_section] = item_count

        total = sum(items_by_section.values())
        is_overloaded = len(violations) > 0

        recs = []
        if is_overloaded:
            recs.append("Add subheadings to group items into 3-5 per group")
            recs.append("Use progressive disclosure for detailed items")
            recs.append("Consider tables for structured data (>7 items)")

        return CognitiveLoadAnalysis(
            total_items=total,
            items_per_section=items_by_section,
            violations=violations,
            is_overloaded=is_overloaded,
            recommendations=recs,
        )

    def enforce(self, text: str, auto_fix: bool = True) -> str:
        """Enforce Miller's limit.

        Args:
            text: Input text
            auto_fix: Auto-fix violations by grouping

        Returns:
            Fixed text
        """
        analysis = self.analyze(text)

        if not analysis.is_overloaded:
            return text

        if not auto_fix:
            return text

        # Auto-fix: group items
        lines = text.split("\n")
        result = []
        item_buffer = []
        item_count = 0

        for line in lines:
            if line.startswith("#"):
                # Flush buffer
                if item_buffer and item_count > self.MILLER_LIMIT:
                    result.extend(self._group_items(item_buffer))
                else:
                    result.extend(item_buffer)

                item_buffer = []
                item_count = 0
                result.append(line)
                continue

            if (
                line.strip().startswith(("-", "*", "•"))
                or re.match(r"^\d+\.\s", line)
            ):
                item_count += 1
                item_buffer.append(line)
            else:
                if item_buffer:
                    if item_count > self.MILLER_LIMIT:
                        result.extend(self._group_items(item_buffer))
                    else:
                        result.extend(item_buffer)
                    item_buffer = []
                    item_count = 0
                result.append(line)

        return "\n".join(result)

    def _group_items(self, items: list[str], group_size: int = 7) -> list[str]:
        """Group items into chunks.

        Args:
            items: List items
            group_size: Max items per group

        Returns:
            Grouped items with section headers
        """
        result = []
        for i in range(0, len(items), group_size):
            group = items[i : i + group_size]
            result.extend(group)

            if i + group_size < len(items):
                result.append("\n### More items\n")

        return result


def enforce_cognitive_load(text: str, auto_fix: bool = True) -> str:
    """Convenience function.

    Args:
        text: Input text
        auto_fix: Auto-fix violations

    Returns:
        Fixed text
    """
    enforcer = CognitiveLoadEnforcer()
    return enforcer.enforce(text, auto_fix)


# ============================================================================
# COMPONENT 6: PROGRESS REPORTING (G6) — LITTLE'S LAW
# ============================================================================


@dataclass
class ProgressReport:
    """Progress report with ETA."""

    current_step: int
    total_steps: int
    elapsed_seconds: float
    eta_seconds: float
    percent_complete: float
    tokens_consumed: int
    tokens_budget: int
    tokens_remaining: int
    status: str  # "running", "stalled", "on_track"


class ProgressTracker:
    """Track query progress using Little's Law.

    L = λW: average items in system = arrival rate × time in system
    """

    def __init__(self, db_path: str = _DEFAULT_DB) -> None:
        """Initialize ProgressTracker.

        Args:
            db_path: Path to sessions database
        """
        self.db_path = db_path
        self.start_time = datetime.now()
        self.observations: list[dict] = []
        self._init_table()

    def _init_table(self) -> None:
        """Initialize progress_tracking table."""
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS progress_tracking (
                    tracking_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    step_num INTEGER NOT NULL,
                    total_steps INTEGER NOT NULL,
                    elapsed_seconds REAL NOT NULL,
                    tokens_consumed INTEGER NOT NULL,
                    tokens_budget INTEGER NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_progress_session "
                "ON progress_tracking(session_id)"
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to initialize progress tracking table: {e}")

    def update(
        self,
        current_step: int,
        total_steps: int,
        tokens_consumed: int,
        tokens_budget: int,
    ) -> ProgressReport:
        """Update progress and forecast ETA.

        Args:
            current_step: Current step number (1-indexed)
            total_steps: Total steps expected
            tokens_consumed: Tokens used so far
            tokens_budget: Total token budget

        Returns:
            ProgressReport with ETA
        """
        now = datetime.now()
        elapsed = (now - self.start_time).total_seconds()

        # Store observation
        self.observations.append({"step": current_step, "elapsed": elapsed, "tokens": tokens_consumed})

        # Forecast ETA using Little's Law
        eta_seconds = self._forecast_eta(current_step, total_steps, tokens_consumed, tokens_budget)

        # Determine status
        if total_steps > 0:
            expected_elapsed = (current_step / total_steps) * (elapsed + eta_seconds)
            if elapsed > expected_elapsed * 1.5:
                status = "stalled"
            else:
                status = "on_track"
        else:
            status = "running"

        tokens_remaining = tokens_budget - tokens_consumed

        return ProgressReport(
            current_step=current_step,
            total_steps=total_steps,
            elapsed_seconds=elapsed,
            eta_seconds=eta_seconds,
            percent_complete=(current_step / total_steps) * 100 if total_steps > 0 else 0,
            tokens_consumed=tokens_consumed,
            tokens_budget=tokens_budget,
            tokens_remaining=tokens_remaining,
            status=status,
        )

    def _forecast_eta(
        self, current_step: int, total_steps: int, tokens_consumed: int, tokens_budget: int
    ) -> float:
        """Forecast ETA using Little's Law + exponential smoothing.

        Args:
            current_step: Current step
            total_steps: Total steps
            tokens_consumed: Tokens used
            tokens_budget: Token budget

        Returns:
            Estimated seconds remaining
        """
        if current_step == 0 or len(self.observations) < 2:
            # No data, assume linear
            avg_tokens_per_step = tokens_consumed / max(1, current_step)
            remaining_tokens = tokens_budget - tokens_consumed
            eta = (remaining_tokens / max(1, avg_tokens_per_step)) * 10  # rough: 10s per step
            return max(0, eta)

        # Exponential smoothing on token rate
        α = 0.3

        recent = self.observations[-1]
        prev = self.observations[-2]

        elapsed_delta = recent["elapsed"] - prev["elapsed"]
        tokens_delta = recent["tokens"] - prev["tokens"]

        if elapsed_delta == 0:
            return 0

        current_rate = tokens_delta / elapsed_delta  # tokens/sec

        # Smooth with previous observations
        if len(self.observations) >= 3:
            prev_prev = self.observations[-3]
            tokens_delta_2 = prev["tokens"] - prev_prev["tokens"]
            elapsed_delta_2 = prev["elapsed"] - prev_prev["elapsed"]
            prev_rate = tokens_delta_2 / max(0.01, elapsed_delta_2)

            current_rate = α * current_rate + (1 - α) * prev_rate

        # Forecast
        tokens_remaining = tokens_budget - tokens_consumed
        time_remaining = tokens_remaining / max(0.1, current_rate)

        return max(0, time_remaining)


def report_progress(
    current_step: int, total_steps: int, tokens_consumed: int, tokens_budget: int
) -> str:
    """Convenience function. Returns human-readable progress string.

    Args:
        current_step: Current step
        total_steps: Total steps
        tokens_consumed: Tokens consumed
        tokens_budget: Token budget

    Returns:
        Progress report string
    """
    tracker = ProgressTracker()
    report = tracker.update(current_step, total_steps, tokens_consumed, tokens_budget)

    return (
        f"Step {report.current_step}/{report.total_steps} "
        f"({report.percent_complete:.0f}%) | "
        f"Elapsed {report.elapsed_seconds:.0f}s | "
        f"ETA {report.eta_seconds:.0f}s | "
        f"Tokens {report.tokens_consumed}/{report.tokens_budget} "
        f"({report.tokens_remaining} left) | "
        f"Status {report.status}"
    )


# ============================================================================
# F6 ORCHESTRATION ENGINE — UNIFIED COMMUNICATION INTERFACE
# ============================================================================


class ComunicacionEngine:
    """Unified communication engine for F6.COMUNICACION.

    Orchestrates:
    - Token counting (exact Anthropic API + fallback)
    - Format adaptation (effort-based output formatting)
    - Cognitive load enforcement (Miller 7±2 rule)
    - Progress reporting (ETA forecasting)
    """

    def __init__(self, api_key: Optional[str] = None, db_path: str = _DEFAULT_DB) -> None:
        """Initialize ComunicacionEngine.

        Args:
            api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var)
            db_path: Path to sessions database
        """
        self.token_counter = TokenCounter(api_key, db_path)
        self.format_selector = FormatSelector()
        self.format_applier = FormatApplier()
        self.cognitive_load_enforcer = CognitiveLoadEnforcer()
        self.progress_tracker = ProgressTracker(db_path)
        self.logger = logging.getLogger(__name__)

    def count_tokens(self, system: str, messages: list[dict], model: str = CLAUDE_MODEL) -> TokenCountResult:
        """Count tokens for a message sequence.

        Args:
            system: System prompt
            messages: List of message dicts
            model: Claude model name

        Returns:
            TokenCountResult with exact token counts
        """
        req = TokenCountRequest(model=model, system_prompt=system, messages=messages)
        return self.token_counter.count_tokens(req)

    def select_format(self, effort_level: EffortLevel, output_tokens: int = 0) -> FormatDirective:
        """Select output format based on effort level.

        Args:
            effort_level: EffortLevel enum
            output_tokens: Optional token count

        Returns:
            FormatDirective with formatting settings
        """
        return self.format_selector.select_format(effort_level, output_tokens)

    def apply_format(self, raw_output: str, effort_level: EffortLevel) -> str:
        """Apply formatting to output based on effort level.

        Args:
            raw_output: Unformatted output
            effort_level: EffortLevel enum

        Returns:
            Formatted output
        """
        directive = self.select_format(effort_level)
        return self.format_applier.apply_format(raw_output, directive)

    def enforce_cognitive_load(self, text: str, auto_fix: bool = True) -> str:
        """Enforce Miller's 7±2 cognitive load limit.

        Args:
            text: Input text
            auto_fix: Auto-fix violations by grouping

        Returns:
            Constrained text
        """
        return self.cognitive_load_enforcer.enforce(text, auto_fix)

    def update_progress(
        self, current_step: int, total_steps: int, tokens_consumed: int, tokens_budget: int
    ) -> ProgressReport:
        """Update progress tracking and forecast ETA.

        Args:
            current_step: Current step number
            total_steps: Total expected steps
            tokens_consumed: Tokens used so far
            tokens_budget: Total token budget

        Returns:
            ProgressReport with ETA and status
        """
        return self.progress_tracker.update(current_step, total_steps, tokens_consumed, tokens_budget)
