"""Programmatic step-debug capture using stdlib ``bdb`` — zero external deps, PHI-safe.

Instruments a target callable (or script) non-interactively: sets breakpoints,
records locals + stack at each hit, captures exception state on throw, enforces
hard bounds (max_hits + timeout), and returns a JSON-serializable result dict.

Intended as the kernel of ARIS4U's escalation capability: run a failing function,
get the captured state, hand the JSON to an LLM for root-cause analysis.

Usage (API)::

    from tools.debug_capture import debug_capture

    result = debug_capture(
        my_func,
        breakpoints=[("/abs/path/to/module.py", 42)],
        inputs=(1, 2),
        max_hits=3,
        timeout_s=10,
    )
    # result["hits"][0]["locals"] → {"x": 1, "y": 2, ...}

Usage (CLI)::

    python -m tools.debug_capture \\
        --target mypackage.module:my_func \\
        --break src/module.py:47 \\
        --break src/module.py:82:"x > 100" \\
        --max-hits 5 \\
        --timeout 30

Limitations (honest):
- ``signal.alarm`` only works in the main thread; non-main-thread tests use a
  threading.Timer fallback that can only interrupt at the next breakpoint hit,
  not mid-loop-without-breakpoints.
- Async functions (``async def``) are not supported; the trace runs synchronously.
- ``sys.settrace`` and bdb are incompatible with ``pytest-cov`` (coverage
  instrumentation conflicts); mark tests with ``@pytest.mark.no_cover`` or run
  separately. The module itself is fine; the conflict is test-runner-level.
- Python 3.12's ``sys.monitoring`` (PEP 669) is more efficient than ``settrace``
  but bdb still uses settrace internally. A future version may migrate.
"""

from __future__ import annotations

import argparse
import bdb
import importlib
import importlib.util
import linecache
import os
import signal
import threading
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

_UNREPRESENTABLE = "<unrepresentable>"
_CIRCULAR = "<circular>"
_SERIALIZE_ERROR = "<error_during_serialization>"


def _safe_serialize(
    obj: Any,
    depth: int = 0,
    *,
    max_depth: int = 3,
    max_vars: int = 30,
    max_repr: int = 200,
    _seen: set[int] | None = None,
) -> Any:
    """Safely convert *obj* to a JSON-serialisable value.

    Handles circular references, non-serialisable objects, and deeply nested
    structures without raising. Clips repr() strings at *max_repr* characters.

    Args:
        obj: Object to serialise.
        depth: Current recursion depth (callers should leave at default 0).
        max_depth: Stop recursing at this depth and return repr() instead.
        max_vars: Maximum dict keys / list items to include.
        max_repr: Maximum length of repr() fallback strings.
        _seen: Internal set of ``id()`` values already visited (circular guard).

    Returns:
        A JSON-compatible value (None, bool, int, float, str, list, or dict).
    """
    if _seen is None:
        _seen = set()

    # Primitives — safe to return as-is (clipping long strings).
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if isinstance(obj, str):
        return obj[:max_repr] + "..." if len(obj) > max_repr else obj

    # Depth guard — fall back to repr at the limit.
    if depth >= max_depth:
        try:
            r = repr(obj)
        except Exception:  # noqa: BLE001
            r = _UNREPRESENTABLE
        return r[:max_repr] if len(r) > max_repr else r

    obj_id = id(obj)
    if obj_id in _seen:
        return _CIRCULAR
    _seen.add(obj_id)

    try:
        if isinstance(obj, dict):
            items = list(obj.items())[:max_vars]
            return {
                str(k): _safe_serialize(
                    v,
                    depth + 1,
                    max_depth=max_depth,
                    max_vars=max_vars,
                    max_repr=max_repr,
                    _seen=_seen,
                )
                for k, v in items
            }

        if isinstance(obj, (list, tuple, set, frozenset)):
            raw = list(obj)[:max_vars]
            serialised = [
                _safe_serialize(
                    item,
                    depth + 1,
                    max_depth=max_depth,
                    max_vars=max_vars,
                    max_repr=max_repr,
                    _seen=_seen,
                )
                for item in raw
            ]
            if isinstance(obj, (tuple, frozenset)):
                return {"__type__": type(obj).__name__, "items": serialised}
            return serialised

        # Generic fallback: repr.
        try:
            r = repr(obj)
        except Exception:  # noqa: BLE001
            r = _UNREPRESENTABLE
        return r[:max_repr] if len(r) > max_repr else r

    except Exception:  # noqa: BLE001
        return _SERIALIZE_ERROR
    finally:
        _seen.discard(obj_id)


def _capture_stack(frame: Any, max_frames: int = 10) -> list[dict[str, Any]]:
    """Walk *frame* upward and return a compact stack representation.

    Args:
        frame: The innermost frame to start from.
        max_frames: Maximum number of frames to include.

    Returns:
        List of dicts with ``file``, ``line``, ``function`` keys (innermost first).
    """
    stack: list[dict[str, Any]] = []
    current = frame
    for _ in range(max_frames):
        if current is None:
            break
        stack.append(
            {
                "file": current.f_code.co_filename,
                "line": current.f_lineno,
                "function": current.f_code.co_name,
            }
        )
        current = current.f_back
    return stack


# ---------------------------------------------------------------------------
# Core debugger class
# ---------------------------------------------------------------------------

_SENTINEL = object()  # distinguishes "no return value" from None


class DebugCapture(bdb.Bdb):
    """Non-interactive bdb sub-class that captures state at breakpoints and exceptions.

    Lifecycle::

        debugger = DebugCapture(max_hits=5, timeout_s=10)
        debugger.set_break(canonical_path, lineno, cond="x > 0")
        result = debugger.run_target(my_func, (arg1,), {"kw": val})

    The ``result`` dict is JSON-serialisable and contains:

    * ``hits`` — list of dicts, one per breakpoint hit (file/function/line/locals/stack).
    * ``hit_count`` — total hits recorded.
    * ``exception`` — dict describing the exception that propagated (if any).
    * ``return_value`` — serialised return value (absent if exception was raised).
    * ``timed_out`` — True if the timeout fired before the target completed.
    """

    def __init__(self, max_hits: int = 5, timeout_s: float = 30.0) -> None:
        """Initialise the capture debugger.

        Args:
            max_hits: Stop after this many breakpoint hits.
            timeout_s: Hard wall-clock timeout in seconds.
        """
        super().__init__()
        self.max_hits = max_hits
        self.timeout_s = timeout_s

        self._hits: list[dict[str, Any]] = []
        self._exception_info: dict[str, Any] | None = None
        self._hit_count: int = 0
        self._timed_out: bool = False
        self._done: bool = False

        # Timeout internals
        self._use_sigalrm: bool = False
        self._old_sigalrm_handler: Any = None
        self._watchdog_timer: threading.Timer | None = None

    # ------------------------------------------------------------------
    # bdb hook overrides
    # ------------------------------------------------------------------

    def dispatch_call(self, frame: Any, arg: Any) -> Any:
        """Override to switch to continue mode immediately after botframe is set.

        bdb's ``reset()`` leaves the debugger in step mode (stop at every line).
        We override the very first ``dispatch_call`` — when ``botframe`` transitions
        from None to a real frame — to call ``set_continue()`` so subsequent
        ``user_line`` calls only fire at registered breakpoints.

        Args:
            frame: The frame being entered.
            arg: Unused by bdb in Python 3.12.

        Returns:
            ``self.trace_dispatch`` or None (per bdb protocol).
        """
        if self.botframe is None:
            # First call: let super set botframe, then immediately continue.
            result = super().dispatch_call(frame, arg)
            if self.botframe is not None:
                self.set_continue()
            return result
        return super().dispatch_call(frame, arg)

    def dispatch_line(self, frame: Any) -> Any:
        """Override to honour ``quitting`` even when no breakpoint stops execution.

        bdb's default ``dispatch_line`` only checks ``self.quitting`` inside the
        ``stop_here or break_here`` branch, so a ``set_quit()`` issued by the
        SIGALRM handler is silently ignored when the target runs in a tight loop
        with no valid breakpoints.  Checking ``quitting`` first makes the timeout
        effective in all cases.

        Args:
            frame: The frame at the current line.

        Returns:
            ``self.trace_dispatch`` to continue tracing, per bdb protocol.
        """
        if self.quitting:
            raise bdb.BdbQuit
        return super().dispatch_line(frame)

    def user_line(self, frame: Any) -> None:
        """Called when a breakpoint is hit (after dispatch_call override takes effect).

        Records file/function/line/locals/stack, then either continues (hit count
        below max) or quits (max_hits reached or timeout fired).

        Args:
            frame: The frame at the breakpoint line.
        """
        if self._timed_out:
            self.set_quit()
            return

        self._hit_count += 1
        self._hits.append(
            {
                "hit": self._hit_count,
                "file": frame.f_code.co_filename,
                "function": frame.f_code.co_name,
                "line": frame.f_lineno,
                "locals": _safe_serialize(dict(frame.f_locals)),
                "stack": _capture_stack(frame),
            }
        )

        if self._hit_count >= self.max_hits:
            self.set_quit()
        else:
            self.set_continue()

    def user_exception(self, frame: Any, exc_info: tuple[Any, Any, Any]) -> None:
        """Belt-and-suspenders exception hook (fires when debugger is already stopped).

        In continue mode the debugger is rarely stopped when an exception fires,
        so ``run_target`` also catches exceptions via a plain try/except.  This
        hook handles the case where the exception occurs at a breakpoint line.

        Args:
            frame: The frame where the exception was raised.
            exc_info: ``(type, value, traceback)`` tuple.
        """
        exc_type, exc_value, _ = exc_info
        self._exception_info = {
            "source": "user_exception_hook",
            "type": exc_type.__name__ if exc_type else "Unknown",
            "message": str(exc_value),
            "file": frame.f_code.co_filename,
            "function": frame.f_code.co_name,
            "line": frame.f_lineno,
            "locals": _safe_serialize(dict(frame.f_locals)),
            "stack": _capture_stack(frame),
        }
        self.set_continue()

    def user_return(self, frame: Any, return_value: Any) -> None:  # noqa: ARG002
        """Called on function return when the debugger is stopped — continue."""
        self.set_continue()

    # ------------------------------------------------------------------
    # Timeout machinery
    # ------------------------------------------------------------------

    def _start_timeout(self) -> None:
        """Arm the timeout watchdog.

        Uses ``signal.SIGALRM`` when running in the main thread (POSIX, precise),
        falls back to a daemon ``threading.Timer`` otherwise (fires a SIGALRM via
        ``os.kill`` from the timer thread, which is delivered to the main thread).
        """
        if threading.current_thread() is threading.main_thread():
            self._use_sigalrm = True
            self._old_sigalrm_handler = signal.signal(signal.SIGALRM, self._sigalrm_handler)
            signal.alarm(max(1, int(self.timeout_s)))
        else:
            self._use_sigalrm = False
            self._watchdog_timer = threading.Timer(self.timeout_s, self._watchdog_fire)
            self._watchdog_timer.daemon = True
            self._watchdog_timer.start()

    def _stop_timeout(self) -> None:
        """Disarm the timeout watchdog."""
        if self._use_sigalrm:
            signal.alarm(0)
            if self._old_sigalrm_handler is not None:
                signal.signal(signal.SIGALRM, self._old_sigalrm_handler)
        if self._watchdog_timer is not None:
            self._watchdog_timer.cancel()

    def _sigalrm_handler(self, signum: int, frame: Any) -> None:  # noqa: ARG002
        """SIGALRM handler: mark timed-out and interrupt the running code.

        Two mechanisms are combined for robustness:

        1. ``set_quit()`` — sets ``self.quitting = True`` so ``dispatch_line``
           raises ``BdbQuit`` on the next breakpoint hit (trace is active).
        2. ``raise bdb.BdbQuit`` — directly raises into whatever bytecode is
           currently executing.  This is the only reliable way to interrupt a
           loop when ``set_continue()`` has removed the trace (which it does
           when no breakpoints are registered).  ``runcall`` catches
           ``BdbQuit`` via its inner ``except BdbQuit: pass``, so the call
           returns cleanly and ``run_target`` records ``timed_out=True``.

        Args:
            signum: Signal number (always SIGALRM here).
            frame: Current frame at interrupt time (unused).
        """
        self._timed_out = True
        self.set_quit()  # covers trace-active path (breakpoints present)
        raise bdb.BdbQuit  # covers no-trace path (no breakpoints, tight loop)

    def _watchdog_fire(self) -> None:
        """Watchdog timer callback for non-main-thread contexts.

        Sets the timed-out flag and sends SIGALRM to the process (which is
        delivered to the main thread by the OS, interrupting the trace).
        """
        if not self._done:
            self._timed_out = True
            try:
                os.kill(os.getpid(), signal.SIGALRM)
            except OSError:
                pass  # best-effort; user_line will check _timed_out on next hit

    # ------------------------------------------------------------------
    # Public runner
    # ------------------------------------------------------------------

    def run_target(
        self,
        target: Any,
        inputs: tuple[Any, ...],
        kwinputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Run *target* under the debugger and return a captured-state dict.

        Calls ``target(*inputs, **kwinputs)`` under bdb tracing. Handles
        ``BdbQuit`` (max_hits / timeout), plain exceptions (postmortem capture),
        and clean returns.

        Args:
            target: Callable to instrument.
            inputs: Positional arguments for *target*.
            kwinputs: Keyword arguments for *target*.

        Returns:
            JSON-serialisable dict with keys ``hits``, ``hit_count``,
            ``timed_out``, and optionally ``exception`` and ``return_value``.
        """
        self._start_timeout()
        return_value: Any = _SENTINEL
        exc_fallback: dict[str, Any] | None = None

        try:
            ret = self.runcall(target, *inputs, **kwinputs)
            return_value = ret
        except bdb.BdbQuit:
            pass  # expected: max_hits reached or timeout fired
        except Exception as exc:  # noqa: BLE001
            # Primary exception capture: walk the traceback to the innermost frame.
            tb = exc.__traceback__
            while tb and tb.tb_next:
                tb = tb.tb_next
            if tb is not None:
                exc_frame = tb.tb_frame
                exc_fallback = {
                    "source": "exception_propagation",
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "file": exc_frame.f_code.co_filename,
                    "function": exc_frame.f_code.co_name,
                    "line": exc_frame.f_lineno,
                    "locals": _safe_serialize(dict(exc_frame.f_locals)),
                    "stack": _capture_stack(exc_frame),
                }
            else:
                exc_fallback = {
                    "source": "exception_propagation",
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
        finally:
            self._done = True
            self._stop_timeout()

        result: dict[str, Any] = {
            "hits": self._hits,
            "hit_count": self._hit_count,
            "timed_out": self._timed_out,
        }
        # user_exception hook takes priority over outer catch (more precise frame).
        exception_info = self._exception_info or exc_fallback
        if exception_info is not None:
            result["exception"] = exception_info
        if return_value is not _SENTINEL:
            result["return_value"] = _safe_serialize(return_value)

        return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def debug_capture(
    target: Any,
    breakpoints: list[tuple[Any, ...]] | None = None,
    inputs: tuple[Any, ...] = (),
    kwinputs: dict[str, Any] | None = None,
    max_hits: int = 5,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Run *target* under programmatic step-debug and return captured state.

    This is the primary public entry point.  Each element of *breakpoints* is
    a 2-tuple ``(file, lineno)`` or a 3-tuple ``(file, lineno, condition_str)``.
    Paths are resolved to absolute canonical form via ``bdb.canonic``.

    Args:
        target: Callable to debug (must be a Python function, not a coroutine).
        breakpoints: List of ``(file, lineno)`` or ``(file, lineno, cond)`` tuples.
            *file* may be relative; it is resolved to an absolute canonical path.
            *cond* is a Python expression string evaluated in the frame's locals.
        inputs: Positional arguments forwarded to *target*.
        kwinputs: Keyword arguments forwarded to *target* (default: ``{}``).
        max_hits: Stop after this many breakpoint hits (default: 5).
        timeout_s: Hard wall-clock timeout in seconds (default: 30).

    Returns:
        JSON-serialisable dict::

            {
                "hits": [
                    {
                        "hit": 1,
                        "file": "/abs/path/to/file.py",
                        "function": "my_func",
                        "line": 42,
                        "locals": {"x": 1, "y": 2},
                        "stack": [{"file": ..., "line": ..., "function": ...}, ...]
                    },
                    ...
                ],
                "hit_count": 3,
                "timed_out": false,
                "exception": {           # only if an exception was raised
                    "type": "ZeroDivisionError",
                    "message": "division by zero",
                    "file": "/abs/path/to/file.py",
                    "function": "my_func",
                    "line": 7,
                    "locals": {"a": 10, "b": 0},
                    "stack": [...]
                },
                "return_value": ...      # only if target returned normally
            }

    Raises:
        ValueError: If a breakpoint tuple has fewer than 2 or more than 3 elements.

    Example::

        def buggy(n):
            return 1 / n      # ZeroDivisionError when n=0

        result = debug_capture(buggy, inputs=(0,))
        assert result["exception"]["type"] == "ZeroDivisionError"
        assert result["exception"]["locals"]["n"] == 0
    """
    if kwinputs is None:
        kwinputs = {}
    if breakpoints is None:
        breakpoints = []

    debugger = DebugCapture(max_hits=max_hits, timeout_s=timeout_s)
    bp_errors: list[str] = []

    for bp in breakpoints:
        if len(bp) == 2:
            bp_file, bp_line = bp
            bp_cond: str | None = None
        elif len(bp) == 3:
            bp_file, bp_line, bp_cond = bp
            bp_cond = bp_cond if bp_cond else None
        else:
            raise ValueError(
                f"Each breakpoint must be (file, lineno) or (file, lineno, cond); got: {bp!r}"
            )

        # Canonicalise via os.path.abspath — intentionally NOT os.path.realpath.
        # bdb.canonic() itself uses abspath (no symlink expansion), so frame
        # filenames and breakpoint paths must both go through the same transform.
        # Using Path.resolve() (realpath) here causes a mismatch on macOS where
        # /var/folders is a symlink to /private/var/folders.
        canonical = debugger.canonic(os.path.abspath(str(bp_file)))
        # Prime linecache so bdb can verify the line exists.
        linecache.getlines(canonical)

        err = debugger.set_break(canonical, int(bp_line), cond=bp_cond)
        if err:
            bp_errors.append(f"{canonical}:{bp_line}: {err}")

    result = debugger.run_target(target, inputs, kwinputs)
    if bp_errors:
        result["breakpoint_errors"] = bp_errors
    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _resolve_target(spec: str) -> Any:
    """Resolve a ``module:attr`` or ``path/to/script.py:func`` spec to a callable.

    Args:
        spec: Target specification in the form ``module.path:func_name`` or
              ``/path/to/script.py:func_name``.

    Returns:
        The resolved callable.

    Raises:
        ValueError: If the spec format is invalid.
        ImportError: If the module or attribute cannot be found.
    """
    if ":" not in spec:
        raise ValueError(
            f"--target must be 'module.path:func' or '/path/to/file.py:func'; got: {spec!r}"
        )
    module_part, func_name = spec.rsplit(":", 1)

    if module_part.endswith(".py") or "/" in module_part or "\\" in module_part:
        # Treat as a file path.
        module_path = Path(module_part).resolve()
        mod_name = module_path.stem
        spec_obj = importlib.util.spec_from_file_location(mod_name, module_path)
        if spec_obj is None or spec_obj.loader is None:
            raise ImportError(f"Cannot load module from {module_path}")
        mod = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(mod)  # type: ignore[union-attr]
    else:
        mod = importlib.import_module(module_part)

    target = getattr(mod, func_name, None)
    if target is None:
        raise ImportError(f"Attribute {func_name!r} not found in {module_part!r}")
    return target


def _parse_breakpoint(raw: str) -> tuple[Any, ...]:
    """Parse a CLI breakpoint string into a tuple.

    Args:
        raw: String in the form ``file:lineno`` or ``file:lineno:condition``.

    Returns:
        Tuple of ``(file, lineno)`` or ``(file, lineno, condition_str)``.

    Raises:
        ValueError: If the string cannot be parsed.
    """
    parts = raw.split(":", 2)
    if len(parts) < 2:
        raise ValueError(f"Breakpoint must be 'file:lineno' or 'file:lineno:cond'; got: {raw!r}")
    try:
        lineno = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"Breakpoint line number must be an integer; got: {parts[1]!r}") from exc

    if len(parts) == 3:
        return (parts[0], lineno, parts[2])
    return (parts[0], lineno)


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: parse args, run debug_capture, print JSON to stdout.

    Args:
        argv: Argument list (defaults to ``sys.argv[1:]`` if None).
    """
    import json

    parser = argparse.ArgumentParser(
        prog="python -m tools.debug_capture",
        description="Non-interactive bdb-based debugger. Captures state at breakpoints and on exception.",
    )
    parser.add_argument(
        "--target",
        required=True,
        metavar="MODULE:FUNC",
        help="Target as 'package.module:function' or '/path/to/file.py:function'.",
    )
    parser.add_argument(
        "--break",
        dest="breakpoints",
        action="append",
        default=[],
        metavar="FILE:LINE[:COND]",
        help="Breakpoint spec. Repeat for multiple. COND is a Python expression.",
    )
    parser.add_argument(
        "--arg",
        dest="args",
        action="append",
        default=[],
        metavar="VALUE",
        help="Positional argument (passed as string; repeat for multiple).",
    )
    parser.add_argument(
        "--max-hits",
        type=int,
        default=5,
        metavar="N",
        help="Maximum breakpoint hits before stopping (default: 5).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="Hard timeout in seconds (default: 30).",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        metavar="N",
        help="JSON indentation for output (default: 2).",
    )

    args = parser.parse_args(argv)

    try:
        target = _resolve_target(args.target)
    except (ValueError, ImportError) as exc:
        parser.error(str(exc))
        return  # unreachable but satisfies type checker

    breakpoints = [_parse_breakpoint(b) for b in args.breakpoints]

    result = debug_capture(
        target,
        breakpoints=breakpoints,
        inputs=tuple(args.args),
        max_hits=args.max_hits,
        timeout_s=args.timeout,
    )
    print(json.dumps(result, indent=args.indent))


if __name__ == "__main__":
    main()
