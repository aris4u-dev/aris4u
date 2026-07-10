"""Stack-specific modules. Each stack.py file exposes:

    COSMETIC_PATTERNS: list[str]     # regex strings
    FUNCTIONAL_PATTERNS: list[str]   # regex strings

Optionally (future):
    TYPE_KEYWORDS: set[str]          # for migration/schema parsers
    TEST_PATH_MARKERS: list[str]     # e.g., ['test/', '_test.dart']
    DEP_CHECKER: callable(repo_root) -> (ok, reason)  # e.g., flutter pub get

Add new stacks by dropping a new .py file here + listing their imports
in `tools/stack_registry/__init__.py` if pre-loading is desired (by
default stacks are lazy-loaded).
"""
