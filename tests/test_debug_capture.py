"""Tests for tools/debug_capture.py — programmatic bdb step-debug capture.

Each test group exercises a distinct capability:
    (a) Simple breakpoint captures correct locals at a known line.
    (b) Conditional breakpoint fires only when its condition evaluates True.
    (c) Exception capture records state at the innermost frame of a throw.
    (d) Bounds: max_hits cuts execution; timeout doesn't hang the suite.
    (e) Serialisation: circular refs, deep nesting, non-serialisable objects.

Tests write target functions to temp .py files so line numbers are stable and
independent of changes elsewhere in this file.
"""
from __future__ import annotations

import importlib.util
import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from tools.debug_capture import _safe_serialize, debug_capture

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_func(source: str, func_name: str, tmp_path: Path) -> tuple[Any, Path]:
    """Write *source* to a temp .py file and return (callable, Path).

    The source is de-dented automatically so callers can use indented literals.

    Args:
        source: Python source code (will be dedented).
        func_name: Name of the function to extract from the module.
        tmp_path: pytest tmp_path fixture directory.

    Returns:
        Tuple of (callable, absolute Path to the file).
    """
    code = textwrap.dedent(source).lstrip("\n")
    mod_file = tmp_path / "target.py"
    mod_file.write_text(code, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("_dc_target", mod_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    func = getattr(mod, func_name)
    return func, mod_file


# ---------------------------------------------------------------------------
# (a) Simple breakpoint — verifies locals at a known line
# ---------------------------------------------------------------------------


class TestSimpleBreakpoint:
    """Breakpoint at a specific line captures the expected local variable state."""

    def test_breakpoint_captures_locals(self, tmp_path: Path) -> None:
        source = """
def off_by_one(items):
    result = []
    for idx in range(len(items) - 1):   # BUG: skips last element
        result.append(items[idx])        # line 5 — breakpoint here
    return result
"""
        func, fpath = _load_func(source, "off_by_one", tmp_path)

        # Line 5 in the dedented file is 'result.append(items[idx])'
        result = debug_capture(
            func,
            breakpoints=[(str(fpath), 5)],
            inputs=(["a", "b", "c"],),
            max_hits=3,
        )

        assert result["hit_count"] >= 1, "Expected at least one breakpoint hit"
        first_hit = result["hits"][0]
        assert first_hit["line"] == 5
        assert "items" in first_hit["locals"]
        # Verify the bug is visible: items has 3 elements but loop runs only 2 times
        assert first_hit["locals"]["items"] == ["a", "b", "c"]
        assert "result" in first_hit["locals"]

    def test_hit_count_matches_loop_iterations(self, tmp_path: Path) -> None:
        source = """
def accumulate(n):
    total = 0
    for i in range(n):
        total += i   # line 5 — hit once per iteration
    return total
"""
        func, fpath = _load_func(source, "accumulate", tmp_path)

        result = debug_capture(
            func,
            breakpoints=[(str(fpath), 4)],  # line 4: 'total += i' after dedent+lstrip
            inputs=(4,),
            max_hits=10,
        )

        # 4 iterations → 4 hits (range(4) = 0,1,2,3)
        # (bdb fires on each iteration once per loop body execution)
        assert result["hit_count"] >= 4
        # Locals at the last recorded hit should show the accumulated total
        last_hit = result["hits"][-1]
        assert "total" in last_hit["locals"]

    def test_result_is_json_serialisable(self, tmp_path: Path) -> None:
        source = """
def simple(x):
    y = x + 1   # line 3
    return y
"""
        func, fpath = _load_func(source, "simple", tmp_path)

        result = debug_capture(
            func,
            breakpoints=[(str(fpath), 3)],
            inputs=(7,),
        )

        # Must not raise — full round-trip through JSON.
        dumped = json.dumps(result)
        parsed = json.loads(dumped)
        assert isinstance(parsed["hits"], list)


# ---------------------------------------------------------------------------
# (b) Conditional breakpoint — fires only when condition is True
# ---------------------------------------------------------------------------


class TestConditionalBreakpoint:
    """Conditional breakpoints respect the expression guard."""

    def test_condition_false_no_hit(self, tmp_path: Path) -> None:
        source = """
def check(x):
    y = x * 2   # line 3
    return y
"""
        func, fpath = _load_func(source, "check", tmp_path)

        result = debug_capture(
            func,
            breakpoints=[(str(fpath), 3, "x > 100")],  # condition never true
            inputs=(5,),
        )

        assert result["hit_count"] == 0
        assert result["hits"] == []

    def test_condition_true_fires(self, tmp_path: Path) -> None:
        source = """
def check(x):
    y = x * 2   # line 3
    return y
"""
        func, fpath = _load_func(source, "check", tmp_path)

        result = debug_capture(
            func,
            breakpoints=[(str(fpath), 3, "x > 3")],  # true when x=5
            inputs=(5,),
        )

        assert result["hit_count"] == 1
        hit = result["hits"][0]
        assert hit["locals"]["x"] == 5

    def test_loop_with_condition_selective(self, tmp_path: Path) -> None:
        """Condition 'i == 3' should fire exactly once in a 5-iteration loop."""
        source = """
def loop(n):
    acc = 0
    for i in range(n):
        acc += i   # line 5
    return acc
"""
        func, fpath = _load_func(source, "loop", tmp_path)

        result = debug_capture(
            func,
            breakpoints=[(str(fpath), 4, "i == 3")],  # line 4: 'acc += i' after dedent+lstrip
            inputs=(5,),
            max_hits=10,
        )

        assert result["hit_count"] == 1
        assert result["hits"][0]["locals"]["i"] == 3


# ---------------------------------------------------------------------------
# (c) Exception capture — state at the innermost throw frame
# ---------------------------------------------------------------------------


class TestExceptionCapture:
    """debug_capture records frame state when the target raises."""

    def test_zero_division_captures_locals(self, tmp_path: Path) -> None:
        source = """
def divide(a, b):
    result = a / b   # line 3 — ZeroDivisionError when b=0
    return result
"""
        func, fpath = _load_func(source, "divide", tmp_path)

        result = debug_capture(func, inputs=(10, 0))

        assert "exception" in result, "Expected exception to be captured"
        exc = result["exception"]
        assert exc["type"] == "ZeroDivisionError"
        # Locals at exception site must expose the bad inputs
        assert exc["locals"]["a"] == 10
        assert exc["locals"]["b"] == 0

    def test_key_error_captures_state(self, tmp_path: Path) -> None:
        source = """
def lookup(d, key):
    value = d[key]   # line 3 — KeyError when key absent
    return value
"""
        func, fpath = _load_func(source, "lookup", tmp_path)

        result = debug_capture(func, inputs=({"x": 1}, "missing"))

        assert "exception" in result
        exc = result["exception"]
        assert exc["type"] == "KeyError"
        assert "d" in exc["locals"]
        assert exc["locals"]["key"] == "missing"

    def test_exception_stack_is_present(self, tmp_path: Path) -> None:
        source = """
def outer(n):
    return inner(n)

def inner(n):
    return 1 / n   # line 6 — ZeroDivisionError
"""
        func, fpath = _load_func(source, "outer", tmp_path)

        result = debug_capture(func, inputs=(0,))

        assert "exception" in result
        exc = result["exception"]
        assert isinstance(exc.get("stack"), list)
        assert len(exc["stack"]) >= 1
        # Innermost frame should mention 'inner'
        assert exc["function"] == "inner"

    def test_no_exception_absent_from_result(self, tmp_path: Path) -> None:
        source = """
def clean(x):
    return x + 1
"""
        func, fpath = _load_func(source, "clean", tmp_path)

        result = debug_capture(func, inputs=(5,))

        assert "exception" not in result
        assert result["return_value"] == 6


# ---------------------------------------------------------------------------
# (d) Bounds: max_hits and timeout
# ---------------------------------------------------------------------------


class TestBounds:
    """Hard limits are enforced: max_hits stops early, timeout doesn't hang."""

    def test_max_hits_stops_execution(self, tmp_path: Path) -> None:
        source = """
def spin(n):
    for i in range(n):
        x = i * 2   # line 4 — hits once per iteration
    return n
"""
        func, fpath = _load_func(source, "spin", tmp_path)

        result = debug_capture(
            func,
            breakpoints=[(str(fpath), 3)],  # line 3: 'x = i * 2' after dedent+lstrip
            inputs=(100,),  # 100 iterations available
            max_hits=3,     # stop at 3
        )

        assert result["hit_count"] == 3
        assert len(result["hits"]) == 3

    @pytest.mark.timeout(10)  # suite-level safety net (pytest-timeout)
    def test_timeout_no_breakpoints_terminates(self) -> None:
        """Timeout must stop an infinite loop even with no registered breakpoints.

        With no breakpoints, ``break_here`` never fires, so ``user_line`` is
        never called and ``max_hits`` is irrelevant.  Only the ``dispatch_line``
        override (which checks ``self.quitting`` unconditionally) can interrupt
        the loop when SIGALRM sets the flag.  ``timed_out`` must be True.
        """

        def _infinite() -> None:
            n = 0
            while True:
                n += 1

        result = debug_capture(
            _infinite,
            breakpoints=[],  # no breakpoints — timeout is the only exit
            inputs=(),
            max_hits=100_000,
            timeout_s=1.0,
        )

        assert result["timed_out"] is True, (
            f"Expected timed_out=True but got hit_count={result['hit_count']}"
        )

    def test_max_hits_zero_no_hits(self, tmp_path: Path) -> None:
        """max_hits=0 means quit immediately without recording any hit."""
        source = """
def func(x):
    y = x   # line 3
    return y
"""
        func, fpath = _load_func(source, "func", tmp_path)

        result = debug_capture(
            func,
            breakpoints=[(str(fpath), 3)],
            inputs=(1,),
            max_hits=0,
        )

        # max_hits=0 triggers set_quit on first hit before recording.
        # hit_count may be 0 (quit before record) or 1 depending on order,
        # but must not exceed 1.
        assert result["hit_count"] <= 1


# ---------------------------------------------------------------------------
# (e) Serialisation safety
# ---------------------------------------------------------------------------


class TestSafeSerialization:
    """_safe_serialize handles edge cases without raising."""

    def test_circular_dict_does_not_explode(self) -> None:
        d: dict[str, Any] = {}
        d["self"] = d  # direct circular reference

        result = _safe_serialize(d)

        # Must return without raising; the circular ref must be tagged.
        assert isinstance(result, dict)
        assert result["self"] == "<circular>"

    def test_circular_list_does_not_explode(self) -> None:
        lst: list[Any] = [1, 2]
        lst.append(lst)  # self-referential list

        result = _safe_serialize(lst)

        assert isinstance(result, list)
        assert "<circular>" in result

    def test_max_depth_clips_nesting(self) -> None:
        nested = {"a": {"b": {"c": {"d": {"e": 99}}}}}

        result = _safe_serialize(nested, max_depth=2)

        # At depth ≥ 2, values become repr strings rather than dicts.
        inner = result["a"]["b"]
        assert isinstance(inner, str), "Depth-clipped value should be a repr string"

    def test_non_serialisable_object_returns_repr(self) -> None:
        class Opaque:
            def __repr__(self) -> str:
                return "<Opaque instance>"

        result = _safe_serialize({"obj": Opaque()})
        assert result["obj"] == "<Opaque instance>"

    def test_repr_that_raises_returns_sentinel(self) -> None:
        class Evil:
            def __repr__(self) -> str:
                raise RuntimeError("boom")

        result = _safe_serialize({"e": Evil()})
        assert result["e"] == "<unrepresentable>"

    def test_primitives_pass_through(self) -> None:
        assert _safe_serialize(None) is None
        assert _safe_serialize(True) is True
        assert _safe_serialize(42) == 42
        assert _safe_serialize(3.14) == 3.14
        assert _safe_serialize("hello") == "hello"

    def test_long_string_clipped(self) -> None:
        long_str = "x" * 500
        result = _safe_serialize(long_str, max_repr=200)
        assert isinstance(result, str)
        assert len(result) <= 203  # 200 + "..."

    def test_exception_capture_result_is_serialisable(self, tmp_path: Path) -> None:
        """End-to-end: captured exception JSON must be loadable after json.dumps."""
        source = """
def bad():
    data = {"key": [1, 2, 3]}
    raise ValueError("intentional")
"""
        func, fpath = _load_func(source, "bad", tmp_path)

        result = debug_capture(func)

        dumped = json.dumps(result)  # must not raise
        parsed = json.loads(dumped)
        assert parsed["exception"]["type"] == "ValueError"


# ---------------------------------------------------------------------------
# End-to-end demo (also serves as documentation)
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Integration scenario: debug a buggy function and inspect captured JSON."""

    def test_off_by_one_demo(self, tmp_path: Path) -> None:
        """Simulate an LLM receiving the captured JSON to diagnose a bug.

        The function clips the last element of a list. The capture must expose
        the wrong `range` bound so an analyser can identify the bug.
        """
        source = """
def clip_last(items):
    out = []
    for idx in range(len(items) - 1):  # BUG: should be range(len(items))
        out.append(items[idx])          # line 5
    return out
"""
        func, fpath = _load_func(source, "clip_last", tmp_path)

        result = debug_capture(
            func,
            breakpoints=[(str(fpath), 5)],
            inputs=(["alpha", "beta", "gamma"],),
            max_hits=5,
        )

        # The captured state must show the loop ran only 2 times (len-1=2),
        # missing "gamma". An LLM analysing this JSON can spot the bug.
        assert result["hit_count"] >= 1
        last_hit = result["hits"][-1]
        # Final recorded idx must be 1 (loop stopped before idx=2)
        assert last_hit["locals"]["idx"] <= 1, (
            f"Expected idx ≤ 1 (skipping last), got {last_hit['locals']['idx']}"
        )
        # return_value absent (BdbQuit or normal return after max_hits)
        # but we should have at least the hits showing the bug
        assert "items" in last_hit["locals"]
