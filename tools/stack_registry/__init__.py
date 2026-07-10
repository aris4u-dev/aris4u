"""ARIS4U stack registry — per-stack dispatch for guardian tools.

H26 infrastructure (2026-04-24): consolidates the stack-bias findings from
C3 Guardianes (F28, F30, F34, F37, F41) behind a single dispatcher. Each
stack (flutter, node_ts, java_spring, python, generic) owns its own
pattern library. Tools like `cosmetic_classifier`, `schema_compat_check`,
`migration_linter`, and `agent_output_verifier` can delegate here instead
of hardcoding patterns inline.

This module only defines the surface. Concrete stack modules live in
`stack_registry/stacks/{stack_name}.py` and are lazy-imported.

Public API:
    detect_stack(file_path) -> str
    get_cosmetic_patterns(file_path) -> (list[str], list[str])
    list_stacks() -> list[str]
"""

from __future__ import annotations

from typing import Tuple, List

from .detect import detect_stack


_STACK_MODULES: dict[str, object] = {}


def _load_stack(name: str) -> object:
    """Lazy-load a stack module by name. Returns the `generic` module
    if the requested stack module doesn't exist.
    """
    if name in _STACK_MODULES:
        return _STACK_MODULES[name]
    try:
        mod = __import__(
            f"stack_registry.stacks.{name}",
            fromlist=[name],
        )
    except ImportError:
        # Fall back to generic for unknown stacks
        try:
            mod = __import__(
                "stack_registry.stacks.generic",
                fromlist=["generic"],
            )
        except ImportError:
            return None
    _STACK_MODULES[name] = mod
    return mod


def get_cosmetic_patterns(file_path: str) -> Tuple[List[str], List[str]]:
    """Return `(cosmetic_patterns, functional_patterns)` regex lists for
    the stack detected at `file_path`. Caller can compose them with its
    preferred regex engine.

    Falls back to `generic` patterns if stack detection fails or the
    stack module doesn't define COSMETIC_PATTERNS.
    """
    stack = detect_stack(file_path)
    mod = _load_stack(stack)
    if mod is None:
        return [], []
    cosmetic = getattr(mod, "COSMETIC_PATTERNS", None)
    functional = getattr(mod, "FUNCTIONAL_PATTERNS", None)
    if cosmetic is None or functional is None:
        # Fall back to generic for incomplete stack modules
        generic = _load_stack("generic")
        if generic is None:
            return [], []
        cosmetic = cosmetic or getattr(generic, "COSMETIC_PATTERNS", [])
        functional = functional or getattr(generic, "FUNCTIONAL_PATTERNS", [])
    return list(cosmetic), list(functional)


def list_stacks() -> List[str]:
    """Return the names of registered stack modules.

    Scans the `stacks/` directory for `.py` files that are not private
    (`_*.py`). Returns sorted list.
    """
    import os
    stacks_dir = os.path.join(os.path.dirname(__file__), "stacks")
    if not os.path.isdir(stacks_dir):
        return []
    names: List[str] = []
    for entry in sorted(os.listdir(stacks_dir)):
        if not entry.endswith(".py"):
            continue
        name = entry[:-3]
        if name.startswith("_"):
            continue
        names.append(name)
    return names


__all__ = ["detect_stack", "get_cosmetic_patterns", "list_stacks"]
