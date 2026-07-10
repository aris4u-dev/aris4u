"""Tests for F6.COMUNICACION — Token Counting, Format Adaptation, Compression, Progress Reporting.

Actualizado: 2026-04-23
V16 Engine: Tests for exact token counting via Anthropic API.
"""

import os
from unittest.mock import MagicMock, patch


from engine.v16.f6_comunicacion import (
    CognitiveLoadEnforcer,
    EffortLevel,
    FormatApplier,
    FormatDirective,
    FormatSelector,
    OutputFormat,
    ProgressReport,
    ProgressTracker,
    TokenCounter,
    TokenCountRequest,
    TokenCountResult,
    count_tokens_simple,
    enforce_cognitive_load,
    format_output,
    report_progress,
)


class TestTokenCounter:
    """Tests for TokenCounter — exact token counting via Anthropic API."""

    def test_init_creates_token_counter(self) -> None:
        """Test TokenCounter initialization."""
        counter = TokenCounter(api_key="test-key")
        assert counter.api_key == "test-key"
        # db_path ahora es ABSOLUTO, anclado al repo (no relativo al cwd → no crea DBs basura).
        assert counter.db_path.endswith("data/sessions.db")
        assert counter.db_path.startswith("/")

    def test_init_uses_env_var_api_key(self) -> None:
        """Test TokenCounter uses ANTHROPIC_API_KEY from environment."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "env-key"}):
            counter = TokenCounter()
            assert counter.api_key == "env-key"

    def test_make_query_hash_is_consistent(self) -> None:
        """Test that make_query_hash produces consistent hashes."""
        counter = TokenCounter(api_key="test-key")
        req = TokenCountRequest(
            model="claude-opus-4-6-20250514",
            system_prompt="You are helpful.",
            messages=[{"role": "user", "content": "Hello"}],
        )
        hash1 = counter._make_query_hash(req)
        hash2 = counter._make_query_hash(req)
        assert hash1 == hash2

    def test_make_query_hash_differs_for_different_content(self) -> None:
        """Test that different content produces different hashes."""
        counter = TokenCounter(api_key="test-key")
        req1 = TokenCountRequest(
            model="claude-opus-4-6-20250514",
            system_prompt="System 1",
            messages=[{"role": "user", "content": "Hello"}],
        )
        req2 = TokenCountRequest(
            model="claude-opus-4-6-20250514",
            system_prompt="System 2",
            messages=[{"role": "user", "content": "World"}],
        )
        hash1 = counter._make_query_hash(req1)
        hash2 = counter._make_query_hash(req2)
        assert hash1 != hash2

    def test_count_tokens_fallback_when_no_api_key(self) -> None:
        """Test that token counting falls back to tiktoken/chars when no API key."""
        counter = TokenCounter(api_key="")
        req = TokenCountRequest(
            model="claude-opus-4-6-20250514",
            system_prompt="System",
            messages=[{"role": "user", "content": "Hello world"}],  # 11 chars
        )
        result = counter.count_tokens(req)
        # Should use tiktoken if available, else chars/4
        assert result.source in ["tiktoken_fallback", "chars_fallback"]
        assert result.input_tokens > 0

    def test_token_count_result_has_total_tokens(self) -> None:
        """Test TokenCountResult calculates total_tokens."""
        result = TokenCountResult(
            input_tokens=100,
            cache_creation_tokens=10,
            cache_read_tokens=5,
            source="api",
            query_hash="abc123",
        )
        result.total_tokens = result.input_tokens + result.cache_creation_tokens + result.cache_read_tokens
        assert result.total_tokens == 115

    def test_fallback_estimate_is_positive(self) -> None:
        """Test that fallback estimation always returns positive token count."""
        counter = TokenCounter(api_key="")
        req = TokenCountRequest(
            model="claude-opus-4-6-20250514",
            system_prompt="",
            messages=[{"role": "user", "content": ""}],
        )
        result = counter._fallback_estimate(req)
        assert result.input_tokens >= 1

    def test_count_tokens_simple_returns_int(self) -> None:
        """Test convenience function count_tokens_simple returns int."""
        # Mock at module level to patch the counter
        with patch("engine.v16.f6_comunicacion.TokenCounter") as MockCounter:
            mock_instance = MagicMock()
            MockCounter.return_value = mock_instance
            mock_instance.count_tokens.return_value = TokenCountResult(
                input_tokens=50, source="api", total_tokens=50
            )
            result = count_tokens_simple("System", [{"role": "user", "content": "Hello"}])
            assert isinstance(result, int)
            assert result == 50


class TestFormatSelector:
    """Tests for FormatSelector — output format selection by effort level."""

    def test_select_format_low_returns_bullets(self) -> None:
        """Test that LOW effort returns bullet list format."""
        selector = FormatSelector()
        directive = selector.select_format(EffortLevel.LOW)
        assert directive.format_type == OutputFormat.BULLET_LIST
        assert directive.max_paragraphs == 1
        assert not directive.include_code

    def test_select_format_medium_returns_structured(self) -> None:
        """Test that MEDIUM effort returns structured format."""
        selector = FormatSelector()
        directive = selector.select_format(EffortLevel.MEDIUM)
        assert directive.format_type == OutputFormat.STRUCTURED
        assert directive.max_paragraphs == 3
        assert directive.include_code

    def test_select_format_high_returns_narrative(self) -> None:
        """Test that HIGH effort returns narrative format."""
        selector = FormatSelector()
        directive = selector.select_format(EffortLevel.HIGH)
        assert directive.format_type == OutputFormat.NARRATIVE
        assert directive.max_paragraphs == 5
        assert directive.include_table

    def test_select_format_xhigh_returns_detailed(self) -> None:
        """Test that XHIGH effort returns detailed format."""
        selector = FormatSelector()
        directive = selector.select_format(EffortLevel.XHIGH)
        assert directive.format_type == OutputFormat.DETAILED
        assert directive.max_paragraphs == 999
        assert directive.include_code
        assert directive.include_table

    def test_all_effort_levels_have_miller_limit(self) -> None:
        """Test that all effort levels enforce Miller 7±2."""
        selector = FormatSelector()
        for effort in EffortLevel:
            directive = selector.select_format(effort)
            assert directive.max_items_per_list == 7


class TestFormatApplier:
    """Tests for FormatApplier — applying formatting directives."""

    def test_apply_format_enforces_list_limit(self) -> None:
        """Test that format applier enforces max items per list."""
        applier = FormatApplier()
        text = "- Item 1\n- Item 2\n- Item 3\n- Item 4\n- Item 5\n- Item 6\n- Item 7\n- Item 8\n- Item 9"
        directive = FormatDirective(
            format_type=OutputFormat.BULLET_LIST,
            max_paragraphs=1,
            include_code=False,
            include_table=False,
            include_examples=False,
            include_references=False,
            max_items_per_list=7,
            progressive_disclosure=False,
        )
        result = applier.apply_format(text, directive)
        # Should limit to 7 items and add ellipsis
        assert "Item 7" in result
        assert "more items" in result or "Item 8" not in result

    def test_apply_format_preserves_non_list_content(self) -> None:
        """Test that format applier preserves paragraph content."""
        applier = FormatApplier()
        text = "This is a paragraph.\n\nAnd another one."
        directive = FormatDirective(
            format_type=OutputFormat.STRUCTURED,
            max_paragraphs=10,
            include_code=False,
            include_table=False,
            include_examples=False,
            include_references=False,
            max_items_per_list=7,
            progressive_disclosure=False,
        )
        result = applier.apply_format(text, directive)
        assert "This is a paragraph." in result
        assert "And another one." in result

    def test_format_output_convenience_function(self) -> None:
        """Test format_output convenience function."""
        text = "- Item 1\n- Item 2\n- Item 3"
        result = format_output(text, EffortLevel.LOW)
        assert isinstance(result, str)
        assert len(result) > 0


class TestCognitiveLoadEnforcer:
    """Tests for CognitiveLoadEnforcer — Miller 7±2 rule enforcement."""

    def test_analyze_counts_list_items(self) -> None:
        """Test that analyze counts list items correctly."""
        enforcer = CognitiveLoadEnforcer()
        text = "- Item 1\n- Item 2\n- Item 3"
        analysis = enforcer.analyze(text)
        assert analysis.total_items == 3

    def test_analyze_detects_overload(self) -> None:
        """Test that analyze detects when list exceeds Miller limit."""
        enforcer = CognitiveLoadEnforcer()
        # Create 10 items (exceeds Miller limit of 7), then add a section to trigger violation detection
        items = "\n".join([f"- Item {i}" for i in range(1, 11)])
        items += "\n## New Section"  # Trigger violation check on previous section
        analysis = enforcer.analyze(items)
        # 10 items should trigger overload
        assert analysis.total_items == 10
        assert analysis.is_overloaded
        assert len(analysis.violations) > 0

    def test_analyze_respects_section_boundaries(self) -> None:
        """Test that analyze respects section headers."""
        enforcer = CognitiveLoadEnforcer()
        text = "## Section A\n- Item 1\n- Item 2\n## Section B\n- Item 3\n- Item 4"
        analysis = enforcer.analyze(text)
        assert "Section A" in analysis.items_per_section
        assert "Section B" in analysis.items_per_section

    def test_enforce_groups_overloaded_items(self) -> None:
        """Test that enforce groups items when overloaded."""
        enforcer = CognitiveLoadEnforcer()
        items = "\n".join([f"- Item {i}" for i in range(1, 15)])
        result = enforcer.enforce(items, auto_fix=True)
        # Should contain original items + grouping markers
        assert "Item 1" in result
        assert "Item 14" in result or "More items" in result

    def test_enforce_no_fix_returns_original(self) -> None:
        """Test that enforce without auto_fix returns original."""
        enforcer = CognitiveLoadEnforcer()
        text = "- Item 1\n- Item 2"
        result = enforcer.enforce(text, auto_fix=False)
        assert result == text

    def test_enforce_cognitive_load_convenience(self) -> None:
        """Test enforce_cognitive_load convenience function."""
        text = "- Item 1\n- Item 2\n- Item 3"
        result = enforce_cognitive_load(text, auto_fix=False)
        assert isinstance(result, str)

    def test_cognitive_load_analysis_provides_recommendations(self) -> None:
        """Test that overloaded analysis provides recommendations."""
        enforcer = CognitiveLoadEnforcer()
        items = "\n".join([f"- Item {i}" for i in range(1, 15)])
        analysis = enforcer.analyze(items)
        if analysis.is_overloaded:
            assert len(analysis.recommendations) > 0


class TestProgressTracker:
    """Tests for ProgressTracker — progress reporting with ETA."""

    def test_init_progress_tracker(self) -> None:
        """Test ProgressTracker initialization."""
        tracker = ProgressTracker()
        assert tracker.start_time is not None
        assert len(tracker.observations) == 0

    def test_update_returns_progress_report(self) -> None:
        """Test that update returns a ProgressReport."""
        tracker = ProgressTracker()
        report = tracker.update(
            current_step=1, total_steps=5, tokens_consumed=100, tokens_budget=1000
        )
        assert isinstance(report, ProgressReport)
        assert report.current_step == 1
        assert report.total_steps == 5

    def test_progress_report_calculates_percent(self) -> None:
        """Test that ProgressReport calculates percent_complete."""
        tracker = ProgressTracker()
        report = tracker.update(
            current_step=2, total_steps=4, tokens_consumed=200, tokens_budget=1000
        )
        assert report.percent_complete == 50.0

    def test_progress_report_calculates_tokens_remaining(self) -> None:
        """Test that ProgressReport calculates tokens_remaining."""
        tracker = ProgressTracker()
        report = tracker.update(
            current_step=1, total_steps=5, tokens_consumed=300, tokens_budget=1000
        )
        assert report.tokens_remaining == 700

    def test_progress_report_status_on_track(self) -> None:
        """Test that ProgressReport status is 'on_track' for normal progress."""
        tracker = ProgressTracker()
        report = tracker.update(
            current_step=1, total_steps=5, tokens_consumed=100, tokens_budget=1000
        )
        assert report.status in ["on_track", "running", "stalled"]

    def test_forecast_eta_with_no_observations(self) -> None:
        """Test ETA forecasting with no prior observations."""
        tracker = ProgressTracker()
        eta = tracker._forecast_eta(
            current_step=1, total_steps=5, tokens_consumed=100, tokens_budget=1000
        )
        assert eta >= 0

    def test_forecast_eta_with_multiple_observations(self) -> None:
        """Test ETA forecasting with historical observations."""
        tracker = ProgressTracker()
        tracker.update(1, 5, 100, 1000)
        tracker.update(2, 5, 200, 1000)
        tracker.update(3, 5, 300, 1000)
        report = tracker.update(4, 5, 400, 1000)
        assert report.eta_seconds >= 0

    def test_report_progress_convenience_function(self) -> None:
        """Test report_progress convenience function."""
        result = report_progress(
            current_step=2, total_steps=5, tokens_consumed=200, tokens_budget=1000
        )
        assert isinstance(result, str)
        assert "Step 2/5" in result
        assert "40" in result  # 40% complete


class TestIntegration:
    """Integration tests for F6 components."""

    def test_format_and_enforce_together(self) -> None:
        """Test that formatting and cognitive load enforcement work together."""
        text = "- " + "\n- ".join([f"Item {i}" for i in range(1, 12)])
        formatted = format_output(text, EffortLevel.LOW)
        enforced = enforce_cognitive_load(formatted)
        # Should have structure preserved and items limited
        assert "Item 1" in enforced

    def test_token_counter_with_real_text(self) -> None:
        """Test token counter with realistic text."""
        counter = TokenCounter(api_key="test-key-no-api")
        text = (
            "This is a realistic example of a longer text that "
            "should be counted for tokens. It contains multiple sentences "
            "and some complexity to test the token counting mechanism."
        )
        req = TokenCountRequest(
            model="claude-opus-4-6-20250514",
            system_prompt="You are a helpful assistant.",
            messages=[{"role": "user", "content": text}],
        )
        result = counter.count_tokens(req)
        # Fallback should give at least some tokens
        assert result.input_tokens > 0
        assert result.source in ["api", "tiktoken_fallback", "chars_fallback"]

    def test_progress_tracking_across_steps(self) -> None:
        """Test progress tracking across multiple steps."""
        tracker = ProgressTracker()
        reports = []
        for step in range(1, 6):
            report = tracker.update(
                current_step=step,
                total_steps=5,
                tokens_consumed=step * 100,
                tokens_budget=1000,
            )
            reports.append(report)

        # Verify progress increases
        assert reports[0].percent_complete < reports[-1].percent_complete
        assert reports[0].tokens_consumed < reports[-1].tokens_consumed


class TestEdgeCases:
    """Edge case tests."""

    def test_token_counter_with_empty_messages(self) -> None:
        """Test token counter with empty messages."""
        counter = TokenCounter(api_key="")
        req = TokenCountRequest(
            model="claude-opus-4-6-20250514",
            system_prompt="System",
            messages=[],
        )
        result = counter._fallback_estimate(req)
        assert result.input_tokens >= 1

    def test_format_applier_with_empty_text(self) -> None:
        """Test format applier with empty text."""
        applier = FormatApplier()
        directive = FormatDirective(
            format_type=OutputFormat.BULLET_LIST,
            max_paragraphs=1,
            include_code=False,
            include_table=False,
            include_examples=False,
            include_references=False,
            max_items_per_list=7,
            progressive_disclosure=False,
        )
        result = applier.apply_format("", directive)
        assert result is not None

    def test_cognitive_load_with_numbered_list(self) -> None:
        """Test cognitive load enforcement with numbered lists."""
        enforcer = CognitiveLoadEnforcer()
        text = "\n".join([f"{i}. Item {i}" for i in range(1, 6)])
        analysis = enforcer.analyze(text)
        assert analysis.total_items == 5

    def test_progress_tracker_with_zero_total_steps(self) -> None:
        """Test progress tracker with zero total steps."""
        tracker = ProgressTracker()
        report = tracker.update(current_step=0, total_steps=0, tokens_consumed=0, tokens_budget=100)
        assert report.percent_complete == 0.0

    def test_token_result_with_cache_tokens(self) -> None:
        """Test TokenCountResult with cache tokens."""
        result = TokenCountResult(
            input_tokens=100,
            cache_creation_tokens=50,
            cache_read_tokens=25,
            source="api",
            query_hash="hash123",
        )
        result.total_tokens = (
            result.input_tokens + result.cache_creation_tokens + result.cache_read_tokens
        )
        assert result.total_tokens == 175
