"""Unit tests for cosmetic_classifier.py

Tests the V16.2 H5 Phase 2 cosmetic diff classifier that determines whether
a code edit is purely cosmetic (colors, fonts, padding) or functional.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


# Add tools to path
tools_path = Path(__file__).parent.parent / "tools"
sys.path.insert(0, str(tools_path))

from cosmetic_classifier import classify


class TestCosmeticClassifierDirect:
    """Direct unit tests of classify() function."""

    def test_pure_cosmetic_color_change(self) -> None:
        """Pure cosmetic: only hex color change."""
        old = "final color = Colors.blue;"
        new = "final color = Color(0xFF1E88E5);"
        ratio = classify(old, new)
        assert ratio == 100, f"Expected 100 for pure cosmetic, got {ratio}"

    def test_pure_cosmetic_padding(self) -> None:
        """Pure cosmetic: only padding changes."""
        old = "padding: EdgeInsets.all(8),"
        new = "padding: EdgeInsets.all(16),"
        ratio = classify(old, new)
        assert ratio == 100, f"Expected 100 for padding-only change, got {ratio}"

    def test_pure_cosmetic_font(self) -> None:
        """Pure cosmetic: font size change."""
        old = "fontSize: 14,"
        new = "fontSize: 18,"
        ratio = classify(old, new)
        assert ratio == 100, f"Expected 100 for font-only change, got {ratio}"

    def test_pure_functional_logic(self) -> None:
        """Pure functional: control flow addition."""
        old = "return user;"
        new = "if (user == null) throw Exception(); return user;"
        ratio = classify(old, new)
        assert ratio == 0, f"Expected 0 for pure functional, got {ratio}"

    def test_pure_functional_database(self) -> None:
        """Pure functional: database query."""
        old = "final data = null;"
        new = 'final data = await db.from("users").select().eq("id", id).single();'
        ratio = classify(old, new)
        assert ratio == 0, f"Expected 0 for DB query, got {ratio}"

    def test_pure_functional_async_await(self) -> None:
        """Pure functional: async/await logic."""
        old = "void load() {}"
        new = "Future<void> load() async { await Future.delayed(Duration(seconds: 1)); }"
        ratio = classify(old, new)
        assert ratio == 0, f"Expected 0 for async, got {ratio}"

    def test_mixed_cosmetic_and_functional_40_percent(self) -> None:
        """Mixed: 2 cosmetic tokens, 3 functional tokens → 40%."""
        # Manual count: "Color(0xFF..." = 1 cosmetic, "padding" = 1 cosmetic
        # "if", "return", "await" = 3 functional
        # Ratio = 2 / (2+3) * 100 = 40
        old = "color: Colors.blue; return null;"
        new = "color: Color(0xFFFF5252); padding: EdgeInsets.all(8); if (x) return await db.select();"
        ratio = classify(old, new)
        # Allow ±10% tolerance for regex variation
        assert 30 <= ratio <= 50, f"Expected ~40% for mixed, got {ratio}%"

    def test_empty_strings(self) -> None:
        """Edge case: empty old_string and new_string."""
        ratio = classify("", "")
        assert ratio == 0, f"Expected 0 for empty, got {ratio}"

    def test_short_combined_length(self) -> None:
        """Edge case: combined < 20 chars returns 0."""
        ratio = classify("x", "y")
        assert ratio == 0, f"Expected 0 for short input, got {ratio}"

    def test_unicode_strings(self) -> None:
        """Unicode content should not crash."""
        old = "label: '标签',"
        new = "label: '标签 Updated',"
        ratio = classify(old, new)
        # Should not crash, ratio doesn't matter for unicode handling
        assert 0 <= ratio <= 100

    def test_no_patterns_matched(self) -> None:
        """Neither cosmetic nor functional patterns match → 0."""
        old = "x = 1 + 2"
        new = "x = 1 + 3"
        ratio = classify(old, new)
        assert ratio == 0, f"Expected 0 when no patterns match, got {ratio}"

    def test_only_whitespace(self) -> None:
        """Only whitespace changes (no tokens)."""
        old = "def foo():\n    pass"
        new = "def foo():\n\n    pass"
        ratio = classify(old, new)
        assert ratio == 0, f"Expected 0 for whitespace-only, got {ratio}"


class TestCosmeticClassifierJSON:
    """Integration tests via JSON payload (mimics hook usage)."""

    def test_via_json_payload_cosmetic(self) -> None:
        """Test via stdin JSON (as hook sees it)."""
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/theme.dart",
                "old_string": "color: Colors.blue",
                "new_string": "color: Color(0xFF2196F3)",
            }
        }
        result = subprocess.run(
            ["python3", str(tools_path / "cosmetic_classifier.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        ratio = int(result.stdout.strip())
        assert ratio == 100, f"Expected 100 via JSON, got {ratio}"

    def test_via_json_payload_functional(self) -> None:
        """Test functional code via JSON."""
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/main.dart",
                "old_string": "var x = 0;",
                "new_string": "var x = 0; if (x > 0) return x; else throw Exception();",
            }
        }
        result = subprocess.run(
            ["python3", str(tools_path / "cosmetic_classifier.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        ratio = int(result.stdout.strip())
        assert ratio == 0, f"Expected 0 via JSON, got {ratio}"

    def test_via_json_malformed_payload(self) -> None:
        """Malformed JSON should default to 0."""
        result = subprocess.run(
            ["python3", str(tools_path / "cosmetic_classifier.py")],
            input="not valid json",
            capture_output=True,
            text=True,
        )
        ratio = int(result.stdout.strip())
        assert ratio == 0, f"Expected 0 for malformed JSON, got {ratio}"

    def test_via_json_missing_strings(self) -> None:
        """Missing old_string/new_string should default to 0."""
        payload = {
            "tool_name": "Edit",
            "tool_input": {
                "file_path": "/tmp/main.dart",
            }
        }
        result = subprocess.run(
            ["python3", str(tools_path / "cosmetic_classifier.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        ratio = int(result.stdout.strip())
        assert ratio == 0, f"Expected 0 for missing strings, got {ratio}"


class TestCosmeticClassifierPatterns:
    """Test specific pattern matching."""

    def test_hex_color_patterns(self) -> None:
        """Hex color patterns should be detected."""
        old = ""
        new = "#FF5252 #FFFFFF #000000"
        ratio = classify(old, new)
        assert ratio == 100, f"Hex colors not detected, got {ratio}"

    def test_dart_color_patterns(self) -> None:
        """Dart Color() patterns should be detected."""
        old = ""
        new = "Color(0xFF2196F3) Color(0xFFFF5252)"
        ratio = classify(old, new)
        assert ratio == 100, f"Dart colors not detected, got {ratio}"

    def test_rgb_rgba_patterns(self) -> None:
        """RGB/RGBA patterns should be detected."""
        old = ""
        new = "rgb(255, 82, 82) rgba(33, 150, 243, 0.5)"
        ratio = classify(old, new)
        assert ratio == 100, f"RGB patterns not detected, got {ratio}"

    def test_functional_if_else(self) -> None:
        """if/else should be functional."""
        old = ""
        new = "if (x > 0) { doSomething(); } else { doOther(); }"
        ratio = classify(old, new)
        assert ratio == 0, f"if/else not detected as functional, got {ratio}"

    def test_functional_database_select(self) -> None:
        """Database .select() should be functional."""
        old = ""
        new = '.from("users").select("id, name").eq("role", "admin")'
        ratio = classify(old, new)
        assert ratio == 0, f"Database query not functional, got {ratio}"

    def test_functional_try_catch(self) -> None:
        """try/catch should be functional."""
        old = ""
        new = "try { risky(); } catch (e) { handle(e); }"
        ratio = classify(old, new)
        assert ratio == 0, f"try/catch not functional, got {ratio}"
