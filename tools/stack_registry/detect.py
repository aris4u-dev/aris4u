"""Stack detection via marker files + file-extension fallback.

Walks up from the given file path looking for canonical marker files
that identify a stack. Returns a short name usable as a module lookup
key.

Priority order (first match wins):
  1. pubspec.yaml                   → "flutter"
  2. pom.xml OR build.gradle*       → "java_spring"
  3. prisma/schema.prisma           → "prisma_ts"
  4. manage.py + settings.py        → "django"
  5. package.json (no prisma)       → "node_ts"
  6. pyproject.toml OR setup.py     → "python"
  7. Cargo.toml                     → "rust"
  8. go.mod                         → "go"
  9. (file extension fallback)
 10. "generic" as ultimate default
"""

from __future__ import annotations

import os


_EXT_TO_STACK: dict[str, str] = {
    ".dart": "flutter",
    ".java": "java_spring",
    ".kt": "java_spring",
    ".kts": "java_spring",
    ".ts": "node_ts",
    ".tsx": "node_ts",
    ".py": "python",
    ".rs": "rust",
    ".go": "go",
    ".astro": "node_ts",
    ".vue": "node_ts",
    ".svelte": "node_ts",
}


def _walk_up(start: str, max_depth: int = 10):
    """Yield directories from `start` up to filesystem root, capped at depth."""
    cur = os.path.dirname(os.path.abspath(start)) if os.path.isfile(start) else os.path.abspath(start)
    for _ in range(max_depth):
        if not cur or cur == "/":
            yield cur
            return
        yield cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return
        cur = parent


def _has_prisma(package_json_path: str) -> bool:
    """Return True if package.json lists prisma as a dep (dev or prod)."""
    try:
        import json
        with open(package_json_path) as f:
            data = json.load(f)
    except Exception:
        return False
    deps = {}
    deps.update(data.get("dependencies") or {})
    deps.update(data.get("devDependencies") or {})
    return any(k == "prisma" or k.startswith("@prisma/") for k in deps)


def _detect_java_spring(d: str) -> bool:
    """Return True if directory `d` holds a Java/Spring marker.

    Args:
        d: Absolute path to a candidate directory.

    Returns:
        True if a Maven (``pom.xml``) or Gradle build file is present.
    """
    if os.path.isfile(os.path.join(d, "pom.xml")):
        return True
    return any(
        os.path.isfile(os.path.join(d, grad))
        for grad in ("build.gradle", "build.gradle.kts", "settings.gradle")
    )


def _detect_prisma(d: str, pkg_json: str) -> bool:
    """Return True if directory `d` is a Prisma/TypeScript project.

    Assumes a ``package.json`` exists at `pkg_json`; checks for a Prisma
    schema file or a declared prisma dependency.

    Args:
        d: Absolute path to a candidate directory.
        pkg_json: Path to the directory's ``package.json``.

    Returns:
        True if a Prisma marker is found.
    """
    if os.path.isfile(os.path.join(d, "prisma", "schema.prisma")):
        return True
    return _has_prisma(pkg_json)


def _detect_django(d: str) -> bool:
    """Return True if directory `d` follows a Django project layout.

    Args:
        d: Absolute path to a candidate directory.

    Returns:
        True if ``manage.py`` and a nested ``settings.py`` are present.
    """
    if not os.path.isfile(os.path.join(d, "manage.py")):
        return False
    for sub in os.listdir(d):
        sub_path = os.path.join(d, sub)
        if os.path.isdir(sub_path) and os.path.isfile(os.path.join(sub_path, "settings.py")):
            return True
    return False


def _detect_python(d: str) -> bool:
    """Return True if directory `d` holds a Python packaging marker.

    Args:
        d: Absolute path to a candidate directory.

    Returns:
        True if ``pyproject.toml`` or ``setup.py`` is present.
    """
    return os.path.isfile(os.path.join(d, "pyproject.toml")) or os.path.isfile(
        os.path.join(d, "setup.py")
    )


def _detect_dir(d: str) -> str | None:
    """Detect the stack for a single directory by marker files.

    Applies the canonical priority order (first match wins) within one
    directory. Caller is responsible for walking the parent chain.

    Args:
        d: Absolute path to a candidate directory.

    Returns:
        A stack name if a marker matched, otherwise None.
    """
    # 1. Flutter
    if os.path.isfile(os.path.join(d, "pubspec.yaml")):
        return "flutter"

    # 2. Java / Spring
    if _detect_java_spring(d):
        return "java_spring"

    # 3. Prisma — check package.json for prisma dep
    pkg_json = os.path.join(d, "package.json")
    has_pkg_json = os.path.isfile(pkg_json)
    if has_pkg_json and _detect_prisma(d, pkg_json):
        return "prisma_ts"

    # 4. Django
    if _detect_django(d):
        return "django"

    # 5. Node / TS — fall-through for package.json without prisma
    if has_pkg_json:
        return "node_ts"

    # 6. Python
    if _detect_python(d):
        return "python"

    # 7. Rust
    if os.path.isfile(os.path.join(d, "Cargo.toml")):
        return "rust"

    # 8. Go
    if os.path.isfile(os.path.join(d, "go.mod")):
        return "go"

    return None


def detect_stack(file_path: str) -> str:
    """Detect the stack of a file via marker files.

    Args:
        file_path: path to any file (or directory). Does not need to exist
                   — only its parent-chain on the filesystem is walked.

    Returns:
        Stack name (e.g. "flutter", "node_ts"). Falls back to extension-based
        guess, then "generic".
    """
    if not file_path:
        return "generic"

    # Walk up looking for markers
    for d in _walk_up(file_path):
        if not d or not os.path.isdir(d):
            continue
        stack = _detect_dir(d)
        if stack is not None:
            return stack

    # Extension-based fallback
    ext = os.path.splitext(file_path)[1].lower()
    if ext in _EXT_TO_STACK:
        return _EXT_TO_STACK[ext]

    return "generic"


__all__ = ["detect_stack"]
