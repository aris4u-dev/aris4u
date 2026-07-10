"""Tests for tools/stack_registry — H26 multi-stack dispatch infrastructure.

Verifies:
- detect_stack() via marker files + extension fallback
- get_cosmetic_patterns() returns per-stack regex lists
- list_stacks() discovers all .py files under stacks/
- Cosmetic classifier is now stack-aware (F28 fix)
"""

from __future__ import annotations

import sys
from pathlib import Path


# Add tools/ to sys.path for direct import
TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))


from stack_registry import detect_stack, get_cosmetic_patterns, list_stacks  # noqa: E402
from cosmetic_classifier import classify  # noqa: E402


class TestDetectStack:
    """detect_stack via marker files + fallback."""

    def test_flutter_via_pubspec(self, tmp_path: Path):
        (tmp_path / "pubspec.yaml").write_text("name: test\n")
        (tmp_path / "lib").mkdir()
        target = tmp_path / "lib" / "main.dart"
        target.write_text("void main() {}")
        assert detect_stack(str(target)) == "flutter"

    def test_java_spring_via_pom(self, tmp_path: Path):
        (tmp_path / "pom.xml").write_text("<project/>")
        target = tmp_path / "Foo.java"
        target.write_text("public class Foo {}")
        assert detect_stack(str(target)) == "java_spring"

    def test_java_spring_via_gradle(self, tmp_path: Path):
        (tmp_path / "build.gradle").write_text("")
        target = tmp_path / "Foo.kt"
        target.write_text("fun main() {}")
        assert detect_stack(str(target)) == "java_spring"

    def test_node_ts_via_package_json(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x","dependencies":{}}')
        target = tmp_path / "src" / "app.tsx"
        target.parent.mkdir()
        target.write_text("export const App = () => null;")
        assert detect_stack(str(target)) == "node_ts"

    def test_prisma_ts_via_schema(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"name":"x"}')
        (tmp_path / "prisma").mkdir()
        (tmp_path / "prisma" / "schema.prisma").write_text("")
        target = tmp_path / "src" / "app.ts"
        target.parent.mkdir()
        target.write_text("const x = 1;")
        assert detect_stack(str(target)) == "prisma_ts"

    def test_python_via_pyproject(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        target = tmp_path / "x.py"
        target.write_text("x = 1")
        assert detect_stack(str(target)) == "python"

    def test_rust_via_cargo(self, tmp_path: Path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname='x'")
        target = tmp_path / "src" / "main.rs"
        target.parent.mkdir()
        target.write_text("fn main(){}")
        assert detect_stack(str(target)) == "rust"

    def test_go_via_gomod(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module x\n")
        target = tmp_path / "main.go"
        target.write_text("package main")
        assert detect_stack(str(target)) == "go"

    def test_extension_fallback_unknown_path(self):
        # No marker files anywhere → extension-based guess
        assert detect_stack("/nonexistent/path/foo.ts") == "node_ts"
        assert detect_stack("/nonexistent/path/foo.dart") == "flutter"

    def test_generic_on_empty(self):
        assert detect_stack("") == "generic"

    def test_generic_on_unknown_extension(self):
        assert detect_stack("/nonexistent/file.xyz") == "generic"


class TestGetCosmeticPatterns:
    """get_cosmetic_patterns returns per-stack regex lists."""

    def test_returns_two_lists(self):
        c, f = get_cosmetic_patterns("/any/path/file.dart")
        assert isinstance(c, list)
        assert isinstance(f, list)
        assert len(c) > 0
        assert len(f) > 0

    def test_flutter_and_python_differ(self, tmp_path: Path):
        (tmp_path / "pubspec.yaml").write_text("")
        flutter_file = tmp_path / "lib" / "w.dart"
        flutter_file.parent.mkdir()
        flutter_file.write_text("")

        (tmp_path.parent / "py_repo").mkdir(exist_ok=True)
        (tmp_path.parent / "py_repo" / "pyproject.toml").write_text("")
        python_file = tmp_path.parent / "py_repo" / "x.py"
        python_file.write_text("")

        c_flutter, _ = get_cosmetic_patterns(str(flutter_file))
        c_python, _ = get_cosmetic_patterns(str(python_file))
        # Patterns should differ between stacks (otherwise H26 is pointless)
        assert c_flutter != c_python


class TestListStacks:
    def test_includes_core_stacks(self):
        stacks = list_stacks()
        assert "flutter" in stacks
        assert "node_ts" in stacks
        assert "java_spring" in stacks
        assert "python" in stacks
        assert "generic" in stacks


class TestCosmeticClassifier:
    """cosmetic_classifier now uses stack registry (F28 fix)."""

    def test_dart_cosmetic_in_flutter_context(self, tmp_path: Path):
        (tmp_path / "pubspec.yaml").write_text("")
        (tmp_path / "lib").mkdir()
        target = tmp_path / "lib" / "w.dart"
        target.write_text("")

        old = "Container(color: Colors.blue)"
        new = (
            "Container(\n"
            "  color: Color(0xFFFFAA00),\n"
            "  padding: EdgeInsets.all(12.0),\n"
            "  decoration: BoxDecoration(borderRadius: BorderRadius.circular(8.0)),\n"
            ")"
        )
        ratio = classify(old, new, str(target))
        assert ratio >= 70, f"expected cosmetic ratio ≥70, got {ratio}"

    def test_dart_cosmetic_in_python_context_is_zero(self):
        # Same cosmetic Dart diff, but with a Python file_path — patterns
        # don't match → 0 ratio. Proves stack isolation (F28 fix).
        old = "Container(color: Colors.blue)"
        new = "Container(color: Color(0xFFFFAA00), padding: EdgeInsets.all(12.0))"
        ratio = classify(old, new, "/some/python/repo/x.py")
        assert ratio == 0, f"expected 0 (Python patterns don't match Dart), got {ratio}"

    def test_ts_tailwind_in_node_context(self):
        old = "<button>Click</button>"
        new = '<button className="bg-blue-500 rounded px-4 py-2 text-white">Click</button>'
        ratio = classify(old, new, "/any/src/comp.tsx")
        assert ratio >= 70, f"expected cosmetic ratio ≥70, got {ratio}"

    def test_functional_diff_is_zero(self):
        old = "x"
        new = "if (x > 0) { await save(); return true; } else { throw Error(); }"
        ratio = classify(old, new, "/any/src/svc.ts")
        assert ratio == 0

    def test_short_diff_is_zero(self):
        # <20 chars → 0 regardless of stack
        assert classify("", "", "/any/file.dart") == 0
        assert classify("a", "b", "/any/file.py") == 0

    def test_no_file_path_uses_generic(self):
        # Empty file_path falls back to generic patterns (backwards compat)
        old = ""
        new = "Color(0xFFFFAA00), padding: EdgeInsets.all(12.0)"
        ratio = classify(old, new, "")
        # Should still detect cosmetic (generic has union of patterns)
        assert ratio >= 50


class TestClassifyPerStack:
    """K3 — per-stack classify() coverage for gaps not in TestCosmeticClassifier.

    java_spring had zero classify() tests; python had only a negative
    isolation test; generic lacked a functional case. Added 2026-07-06.
    """

    def test_java_spring_cosmetic(self, tmp_path: Path) -> None:
        """CSS-in-Thymeleaf colours + spacing → cosmetic ratio >=70."""
        (tmp_path / "pom.xml").write_text("<project/>")
        target = tmp_path / "app.css"
        target.write_text("")
        old = "color: #FFFFFF; padding: 8px;"
        new = (
            "color: #FF5252; padding: 16px; margin: 4px;"
            " border-radius: 4px; font-size: 14px;"
        )
        ratio = classify(old, new, str(target))
        assert ratio >= 70, f"java_spring cosmetic expected >=70, got {ratio}"

    def test_java_spring_functional(self, tmp_path: Path) -> None:
        """Spring annotations + try/catch/throw → functional (ratio 0)."""
        (tmp_path / "pom.xml").write_text("<project/>")
        target = tmp_path / "UserController.java"
        target.write_text("")
        old = "public String get() { return null; }"
        new = (
            "@GetMapping public ResponseEntity<User> get() { "
            "try { return ResponseEntity.ok(service.find()); } "
            "catch (Exception e) { throw new RuntimeException(e); } }"
        )
        ratio = classify(old, new, str(target))
        assert ratio == 0, f"java_spring functional expected 0, got {ratio}"

    def test_python_cosmetic(self, tmp_path: Path) -> None:
        """Matplotlib styling calls → cosmetic ratio >=70."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        target = tmp_path / "chart.py"
        target.write_text("")
        old = "plt.plot(x, y)"
        new = (
            "plt.set_title('Dashboard'); plt.set_facecolor('#F5F5F5');"
            " plt.set_xlabel('Date'); fontsize=14; color='#2196F3'"
        )
        ratio = classify(old, new, str(target))
        assert ratio >= 70, f"python cosmetic expected >=70, got {ratio}"

    def test_python_functional(self, tmp_path: Path) -> None:
        """async def + await + try/except/raise → functional (ratio 0)."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        target = tmp_path / "service.py"
        target.write_text("")
        old = "data = None"
        new = (
            "async def fetch(cid: int):"
            " await validate(cid);"
            " try: return await db.get(cid)"
            " except Exception as e: raise ValueError(str(e))"
        )
        ratio = classify(old, new, str(target))
        assert ratio == 0, f"python functional expected 0, got {ratio}"

    def test_generic_functional(self) -> None:
        """Generic stack: control flow + await + throw → functional (ratio 0)."""
        old = ""
        new = (
            "if (x > 0) { await save(x); return result; }"
            " else { throw Error('validation failed'); }"
        )
        ratio = classify(old, new, "")
        assert ratio == 0, f"generic functional expected 0, got {ratio}"
