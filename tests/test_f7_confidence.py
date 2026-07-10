"""Tests for F7 confidence exposure helper — format_confidence_reminder."""


from engine.v16.f7_aprendizaje import format_confidence_reminder


class TestFormatConfidenceReminder:
    """Test format_confidence_reminder helper function."""

    def test_high_confidence_score(self):
        """Score >= 0.9 returns HIGH level."""
        result = format_confidence_reminder(0.95, "decision")
        assert "95%" in result
        assert "HIGH" in result
        assert "decision" in result

    def test_medium_confidence_score(self):
        """Score 0.7-0.89 returns MEDIUM level."""
        result = format_confidence_reminder(0.75, "f5_validation")
        assert "75%" in result
        assert "MEDIUM" in result
        assert "f5_validation" in result

        result2 = format_confidence_reminder(0.85, "voting")
        assert "85%" in result2
        assert "MEDIUM" in result2

    def test_low_confidence_score(self):
        """Score < 0.7 returns LOW level."""
        result = format_confidence_reminder(0.45, "research")
        assert "45%" in result
        assert "LOW" in result
        assert "research" in result

    def test_boundary_0_9(self):
        """Score exactly 0.9 returns HIGH."""
        result = format_confidence_reminder(0.9, "boundary test")
        assert "90%" in result
        assert "HIGH" in result

    def test_boundary_0_7(self):
        """Score exactly 0.7 returns MEDIUM."""
        result = format_confidence_reminder(0.7, "boundary test")
        assert "70%" in result
        assert "MEDIUM" in result

    def test_zero_confidence(self):
        """Score 0.0 returns LOW."""
        result = format_confidence_reminder(0.0, "worst case")
        assert "0%" in result
        assert "LOW" in result

    def test_full_confidence(self):
        """Score 1.0 returns HIGH."""
        result = format_confidence_reminder(1.0, "perfect")
        assert "100%" in result
        assert "HIGH" in result

    def test_default_context(self):
        """Default context is 'decision'."""
        result = format_confidence_reminder(0.8)
        assert "80%" in result
        assert "MEDIUM" in result
        assert "decision" in result

    def test_custom_context(self):
        """Custom context is included in output."""
        result = format_confidence_reminder(
            0.75, "custom validation phase"
        )
        assert "custom validation phase" in result
        assert "75%" in result

    def test_format_contains_brackets(self):
        """Output is bracketed for system-reminder injection."""
        result = format_confidence_reminder(0.8, "test")
        assert result.startswith("[")
        assert "]" in result

    def test_percentage_formatting(self):
        """Confidence is formatted as percentage without decimals."""
        result = format_confidence_reminder(0.123, "test")
        assert "12%" in result  # Rounds to 12%

        result2 = format_confidence_reminder(0.876, "test")
        assert "88%" in result2  # Rounds to 88%


class TestConfidenceRemindersSequence:
    """Test realistic sequences of confidence reminders."""

    def test_research_loop_sequence(self):
        """Multiple queries at different confidence levels."""
        queries = [
            (0.92, "query_1"),
            (0.76, "query_2"),
            (0.45, "query_3"),
            (0.88, "query_4"),
        ]

        reminders = [
            format_confidence_reminder(score, f"research_{i}")
            for i, (score, _) in enumerate(queries)
        ]

        assert "HIGH" in reminders[0]
        assert "MEDIUM" in reminders[1]
        assert "LOW" in reminders[2]
        assert "MEDIUM" in reminders[3]

    def test_system_reminder_injection(self):
        """Format is suitable for system-reminder context injection."""
        result = format_confidence_reminder(0.83, "decision point")
        expected_format = "[Confidence: 83% MEDIUM] decision point"
        assert result == expected_format
