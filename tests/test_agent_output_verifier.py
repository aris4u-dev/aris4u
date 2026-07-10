"""Unit tests for tools/agent_output_verifier.py (V16.3 H19+H20).

Verifies the verifier catches the two Fase B patterns where the agent
self-reported success but reality differed:
  - Flutter `pub get` resolution failure silently ignored.
  - Dart test files with `late T foo;` never assigned in setUp (would throw
    LateInitializationError on first call — a broken test).
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path
from tempfile import TemporaryDirectory


TOOLS = Path(__file__).parent.parent / "tools"
VERIFIER = TOOLS / "agent_output_verifier.py"

# In-process import of the module for unit-level characterization of compile_file's
# per-stack branches (dart/java/ts/else), which the subprocess-based tests above
# cannot reach without real flutter/mvn/npx binaries + marker files present.
sys.path.insert(0, str(TOOLS))
import agent_output_verifier as aov  # noqa: E402


def run_verifier(repo: Path, files: list[Path]) -> tuple[int, dict]:
    args = ["python3", str(VERIFIER), str(repo), *(str(f) for f in files)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        result = {"_parse_error": proc.stdout, "_stderr": proc.stderr}
    return proc.returncode, result


class TestInvalidArgs:
    def test_missing_repo_arg_exits_2(self) -> None:
        proc = subprocess.run(
            ["python3", str(VERIFIER)],
            capture_output=True, text=True,
        )
        assert proc.returncode == 2

    def test_nonexistent_repo_exits_2(self) -> None:
        proc = subprocess.run(
            ["python3", str(VERIFIER), "/nonexistent/path"],
            capture_output=True, text=True,
        )
        assert proc.returncode == 2


class TestEmptyFileListOK:
    def test_no_files_reports_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            rc, res = run_verifier(Path(tmp), [])
            assert rc == 0
            assert res["files_total"] == 0
            assert res["verified"] == 0
            assert res["errors"] == []


class TestCompileChecks:
    def test_valid_python_passes(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "good.py"
            f.write_text("x = 1\nprint(x)\n")
            rc, res = run_verifier(Path(tmp), [f])
            assert rc == 0
            assert res["verified"] == 1
            assert res["errors"] == []

    def test_syntax_error_python_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "bad.py"
            f.write_text("def foo(:\n    pass\n")
            rc, res = run_verifier(Path(tmp), [f])
            assert rc == 1
            assert res["verified"] == 0
            categories = {e["category"] for e in res["errors"]}
            assert "compile_error" in categories

    def test_valid_bash_passes(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "good.sh"
            f.write_text("#!/bin/bash\necho hello\n")
            rc, res = run_verifier(Path(tmp), [f])
            assert rc == 0
            assert res["verified"] == 1

    def test_syntax_error_bash_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "bad.sh"
            f.write_text("#!/bin/bash\nif [ 1 = 1 ]\n  echo missing then\nfi\n")
            rc, res = run_verifier(Path(tmp), [f])
            assert rc == 1
            assert any(e["category"] == "compile_error" for e in res["errors"])


class TestBrokenTestLateUninit:
    """H20 — detect test files where `late X var;` is declared but never assigned."""

    def test_late_uninit_in_test_flagged(self) -> None:
        with TemporaryDirectory() as tmp:
            testdir = Path(tmp) / "test" / "services"
            testdir.mkdir(parents=True)
            broken = testdir / "payment_service_test.dart"
            broken.write_text("""
import 'package:flutter_test/flutter_test.dart';
class PaymentService {}
void main() {
  late PaymentService paymentService;
  setUp(() {
    // paymentService NOT assigned — this is the Fase B bug.
  });
  test('something', () {
    paymentService.doStuff();  // will throw LateInitializationError
  });
}
""")
            rc, res = run_verifier(Path(tmp), [broken])
            assert rc == 1
            broken_tests = res.get("broken_tests") or []
            assert len(broken_tests) == 1
            assert "payment_service_test.dart" in broken_tests[0]
            cats = {e["category"] for e in res["errors"]}
            assert "broken_test_late_uninit" in cats

    def test_late_with_assignment_is_ok(self) -> None:
        with TemporaryDirectory() as tmp:
            testdir = Path(tmp) / "test"
            testdir.mkdir()
            good = testdir / "good_test.dart"
            good.write_text("""
import 'package:flutter_test/flutter_test.dart';
class Foo { void run() {} }
void main() {
  late Foo foo;
  setUp(() {
    foo = Foo();
  });
  test('ok', () {
    foo.run();
  });
}
""")
            rc, res = run_verifier(Path(tmp), [good])
            # No broken_test error for this file.
            broken_tests = res.get("broken_tests") or []
            assert broken_tests == [], f"unexpected: {broken_tests}"

    def test_late_outside_test_folder_ignored(self) -> None:
        """Production code with `late X var;` should NOT be flagged."""
        with TemporaryDirectory() as tmp:
            libdir = Path(tmp) / "lib" / "services"
            libdir.mkdir(parents=True)
            src = libdir / "my_service.dart"
            src.write_text("""
class MyService {
  late String _name;
  void init(String name) {
    _name = name;
  }
}
""")
            rc, res = run_verifier(Path(tmp), [src])
            # Production file — heuristic only flags test files.
            # Also: _name IS assigned (in init).
            broken_tests = res.get("broken_tests") or []
            assert broken_tests == []


class TestFileFilteringAndSecurity:
    def test_files_outside_repo_ignored(self) -> None:
        with TemporaryDirectory() as outside, TemporaryDirectory() as repo:
            outside_file = Path(outside) / "malicious.py"
            outside_file.write_text("# do not verify this\n")
            rc, res = run_verifier(Path(repo), [outside_file])
            assert res["files_total"] == 0  # filtered by relative_to guard

    def test_nonexistent_files_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            fake = Path(tmp) / "does_not_exist.py"
            rc, res = run_verifier(Path(tmp), [fake])
            assert res["files_total"] == 0


class TestMultiStackDispatch:
    """H26 / F37 — verifier dispatches dependency check by detected stack.

    Pre-fix, V16.2 verifier always ran flutter pub-check, so Java/TS repos
    silently skipped (pubspec absent → ok=True). Post-fix, each stack gets
    its own dependency tool: maven for Java, npm for TS, etc.
    """

    def test_java_repo_detects_stack_via_pom(self) -> None:
        """A .java file alongside pom.xml → stack=java_spring."""
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "pom.xml").write_text(
                "<project><modelVersion>4.0.0</modelVersion>"
                "<groupId>x</groupId><artifactId>y</artifactId>"
                "<version>1</version></project>\n"
            )
            src = Path(tmp) / "src" / "main" / "java"
            src.mkdir(parents=True)
            j = src / "Hello.java"
            j.write_text("class Hello {}\n")
            rc, res = run_verifier(Path(tmp), [j])
            assert res["stack"] == "java_spring"
            # Dependency check fires (may report mvn missing or other error).
            # Key invariant: the field exists and is meaningful.
            assert "dependency_ok" in res
            assert "dependency_reason" in res

    def test_node_ts_repo_detects_stack_via_package_json(self) -> None:
        """A .ts file alongside package.json → stack=node_ts."""
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text(
                '{"name":"x","version":"1.0.0"}\n'
            )
            t = Path(tmp) / "a.ts"
            t.write_text("const x: number = 1; export default x;\n")
            rc, res = run_verifier(Path(tmp), [t])
            assert res["stack"] == "node_ts"
            assert "dependency_ok" in res

    def test_python_repo_skips_dependency_check_with_reason(self) -> None:
        """Python venvs are heterogeneous → dep check skipped with explicit reason."""
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "pyproject.toml").write_text(
                '[project]\nname = "x"\nversion = "0.1"\n'
            )
            p = Path(tmp) / "a.py"
            p.write_text("x = 1\n")
            rc, res = run_verifier(Path(tmp), [p])
            assert res["stack"] == "python"
            assert res["dependency_ok"] is True
            assert "venv" in res["dependency_reason"].lower() or "skip" in res["dependency_reason"].lower()

    def test_stack_field_present_in_output(self) -> None:
        """Every run must report the detected stack in the JSON output."""
        with TemporaryDirectory() as tmp:
            f = Path(tmp) / "x.py"
            f.write_text("x = 1\n")
            rc, res = run_verifier(Path(tmp), [f])
            assert "stack" in res
            assert isinstance(res["stack"], str)


class TestPubResolutionCheck:
    """Pub resolution only runs if project has pubspec.yaml AND any file changed is .dart.

    We don't test live pub here (would need network / real flutter SDK); we
    check the shape of the response for common configurations.
    """

    def test_no_dart_files_skips_pub_check(self) -> None:
        """H26: a Python-only changeset detects stack=python and skips
        Flutter-specific pub check (returns ok=True with python reason)."""
        with TemporaryDirectory() as tmp:
            py = Path(tmp) / "a.py"
            py.write_text("x = 1\n")
            rc, res = run_verifier(Path(tmp), [py])
            assert res["pub_ok"] is True  # backward-compat alias
            assert res["dependency_ok"] is True
            assert res["stack"] == "python"

    def test_no_pubspec_skips_pub_check(self) -> None:
        """H26: a .dart file without pubspec.yaml still detects stack=flutter
        via extension fallback, but pub check skips with 'no pubspec' reason."""
        with TemporaryDirectory() as tmp:
            d = Path(tmp) / "a.dart"
            d.write_text("void main() {}\n")
            rc, res = run_verifier(Path(tmp), [d])
            assert res["pub_ok"] is True
            assert res["dependency_ok"] is True
            assert res["stack"] == "flutter"
            assert "pubspec" in res["dependency_reason"].lower() or "skipped" in res["dependency_reason"].lower()


def _fake_proc(returncode: int, stdout: str = "", stderr: str = "") -> types.SimpleNamespace:
    """Build a stand-in for subprocess.CompletedProcess (only fields compile_file reads)."""
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class TestCompileFileBranches:
    """Characterization of compile_file()'s per-stack branches.

    These pin the EXACT behavior (commands, result parsing, skip conditions,
    return values) of the dart/java/ts/else paths before refactoring. They mock
    subprocess.run / shutil.which / _find_marker_root because the real toolchains
    and marker files are not present in the test environment.
    """

    def test_nonexistent_file_returns_none(self) -> None:
        assert aov.compile_file(Path("/nope/does_not_exist.py")) is None

    def test_unknown_suffix_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "readme.md"
        f.write_text("# hi\n")
        assert aov.compile_file(f) is None

    # --- Dart branch ------------------------------------------------------
    def test_dart_skips_when_no_pubspec(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.dart"
        f.write_text("void main() {}\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: None)
        assert aov.compile_file(f) is None

    def test_dart_skips_when_flutter_missing(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.dart"
        f.write_text("void main() {}\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: tmp_path)
        monkeypatch.setattr(aov.shutil, "which", lambda _: None)
        assert aov.compile_file(f) is None

    def test_dart_error_lines_parsed(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.dart"
        f.write_text("void main() {}\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: tmp_path)
        monkeypatch.setattr(aov.shutil, "which", lambda _: "/usr/bin/flutter")
        stdout = "error • Undefined name 'x' • lib/a.dart:1:1\nwarning • unused"
        monkeypatch.setattr(aov.subprocess, "run", lambda *a, **k: _fake_proc(1, stdout=stdout))
        out = aov.compile_file(f)
        assert out is not None
        assert "error •" in out
        assert "warning" not in out

    def test_dart_clean_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.dart"
        f.write_text("void main() {}\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: tmp_path)
        monkeypatch.setattr(aov.shutil, "which", lambda _: "/usr/bin/flutter")
        monkeypatch.setattr(aov.subprocess, "run", lambda *a, **k: _fake_proc(0, stdout="No issues found!"))
        assert aov.compile_file(f) is None

    # --- Java / Kotlin branch --------------------------------------------
    def test_java_skips_when_no_pom_or_mvn(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "Hello.java"
        f.write_text("class Hello {}\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: None)
        assert aov.compile_file(f) is None

    def test_java_mvn_failure_parsed(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "Hello.java"
        f.write_text("class Hello {}\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: tmp_path)
        monkeypatch.setattr(aov.shutil, "which", lambda _: "/usr/bin/mvn")
        stdout = "[ERROR] cannot find symbol\n[INFO] noise"
        monkeypatch.setattr(aov.subprocess, "run", lambda *a, **k: _fake_proc(1, stdout=stdout))
        out = aov.compile_file(f)
        assert out is not None
        assert out.startswith("mvn compile failed:")
        assert "cannot find symbol" in out

    def test_java_mvn_clean_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "Hello.java"
        f.write_text("class Hello {}\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: tmp_path)
        monkeypatch.setattr(aov.shutil, "which", lambda _: "/usr/bin/mvn")
        monkeypatch.setattr(aov.subprocess, "run", lambda *a, **k: _fake_proc(0, stdout="BUILD SUCCESS"))
        assert aov.compile_file(f) is None

    # --- TS / JS branch ---------------------------------------------------
    def test_ts_skips_when_no_tsconfig(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.ts"
        f.write_text("const x: number = 1;\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: None)
        assert aov.compile_file(f) is None

    def test_ts_skips_when_npx_missing(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.ts"
        f.write_text("const x: number = 1;\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: tmp_path)
        monkeypatch.setattr(aov.shutil, "which", lambda _: None)
        assert aov.compile_file(f) is None

    def test_ts_tsc_failure_parsed(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.ts"
        f.write_text("const x: number = 'oops';\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: tmp_path)
        monkeypatch.setattr(aov.shutil, "which", lambda _: "/usr/bin/npx")
        stdout = "a.ts(1,7): error TS2322: Type 'string' is not assignable\nfoo"
        monkeypatch.setattr(aov.subprocess, "run", lambda *a, **k: _fake_proc(2, stdout=stdout))
        out = aov.compile_file(f)
        assert out is not None
        assert out.startswith("tsc failed:")
        assert "error TS" in out

    def test_ts_tsc_clean_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.ts"
        f.write_text("const x: number = 1;\n")
        monkeypatch.setattr(aov, "_find_marker_root", lambda *_: tmp_path)
        monkeypatch.setattr(aov.shutil, "which", lambda _: "/usr/bin/npx")
        monkeypatch.setattr(aov.subprocess, "run", lambda *a, **k: _fake_proc(0, stdout=""))
        assert aov.compile_file(f) is None

    # --- Timeout / exception handling ------------------------------------
    def test_timeout_returns_timeout_message(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")

        def _raise(*a, **k):
            raise subprocess.TimeoutExpired(cmd="py_compile", timeout=20)

        monkeypatch.setattr(aov.subprocess, "run", _raise)
        out = aov.compile_file(f)
        assert out is not None
        assert out.startswith("timeout compiling")

    def test_generic_exception_returns_exception_message(self, tmp_path: Path, monkeypatch) -> None:
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")

        def _raise(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(aov.subprocess, "run", _raise)
        out = aov.compile_file(f)
        assert out is not None
        assert out.startswith("exception compiling")
        assert "RuntimeError" in out
