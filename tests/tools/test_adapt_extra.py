"""Cobertura para tools/adapt/watch_sources.py y tools/adapt/smoke_test.py.

watch_sources = vigía de cambios de Claude: hash-diff de versión/changelog/settings
+ id de modelo, clasificando cada delta en 'mechanical' vs 'semantic'. Aquí se prueba
la clasificación con inputs sintéticos + la lectura de estado fail-open + current_state
con subprocess mockeado.

smoke_test = el GATE de 4 checks (MCP tools / backend sqlite / hooks .sh / token-counting)
que debe ser fail-closed. Aquí se prueba que cada check pasa/falla con sus dependencias
mockeadas, sin tocar el sistema real, y que main() agrega y propaga el exit-code correcto.

Patrón (como el resto de tests/tools): importar el módulo directo y mockear
subprocess/red/imports con unittest.mock + monkeypatch. NO se tocan DBs/logs reales
(las fixtures autouse de conftest aíslan; aquí además se mockea todo lo externo).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

import tools.adapt.smoke_test as smoke
import tools.adapt.watch_sources as watch


# ===========================================================================
# watch_sources._hash_file
# ===========================================================================
class TestHashFile:
    """_hash_file: sha256 truncado a 16 hex, fail-open ('') si no se puede leer."""

    def test_hash_of_known_bytes_is_stable_and_16_hex(self, tmp_path: Path) -> None:
        f = tmp_path / "a.txt"
        f.write_bytes(b"hello aris")
        h1 = watch._hash_file(f)
        h2 = watch._hash_file(f)
        assert h1 == h2  # determinista
        assert len(h1) == 16
        assert all(c in "0123456789abcdef" for c in h1)

    def test_different_content_yields_different_hash(self, tmp_path: Path) -> None:
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_bytes(b"contenido A")
        b.write_bytes(b"contenido B distinto")
        assert watch._hash_file(a) != watch._hash_file(b)

    def test_missing_file_fails_open_to_empty(self, tmp_path: Path) -> None:
        missing = tmp_path / "no-existe.md"
        assert watch._hash_file(missing) == ""

    def test_directory_path_fails_open(self, tmp_path: Path) -> None:
        # read_bytes sobre un directorio lanza -> fail-open a ''
        assert watch._hash_file(tmp_path) == ""


# ===========================================================================
# watch_sources.load_state
# ===========================================================================
class TestLoadState:
    """load_state: lee data/adapt_state.json; fail-open a {} si falta o está corrupto."""

    def test_missing_state_returns_empty_dict(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(watch, "STATE_FILE", tmp_path / "adapt_state.json")
        assert watch.load_state() == {}

    def test_valid_state_is_parsed(self, tmp_path: Path, monkeypatch) -> None:
        sf = tmp_path / "adapt_state.json"
        payload = {"claude_version": "1.2.3", "claude_model": "opus-4-8"}
        sf.write_text(json.dumps(payload))
        monkeypatch.setattr(watch, "STATE_FILE", sf)
        assert watch.load_state() == payload

    def test_corrupt_json_fails_open_to_empty(self, tmp_path: Path, monkeypatch) -> None:
        sf = tmp_path / "adapt_state.json"
        sf.write_text("{ esto no es json valido :::")
        monkeypatch.setattr(watch, "STATE_FILE", sf)
        assert watch.load_state() == {}


# ===========================================================================
# watch_sources.diff  — clasificación mechanical vs semantic (inputs sintéticos)
# ===========================================================================
class TestDiffClassification:
    """diff: por cada clave de ROUTE que cambió, emite un delta con su ruta.

    Contrato de ROUTE (determinista):
      claude_version  -> mechanical
      claude_model    -> mechanical
      settings_hash   -> mechanical
      changelog_hash  -> semantic
    """

    def test_no_change_yields_no_deltas(self) -> None:
        state = {
            "claude_version": "1.0.0",
            "claude_model": "opus-4-8",
            "settings_hash": "aaaa",
            "changelog_hash": "bbbb",
        }
        assert watch.diff(state, dict(state)) == []

    def test_version_bump_is_mechanical(self) -> None:
        prev = {"claude_version": "1.0.0"}
        cur = {"claude_version": "1.1.0"}
        deltas = watch.diff(prev, cur)
        ver = [d for d in deltas if d["source"] == "claude_version"]
        assert len(ver) == 1
        assert ver[0]["route"] == "mechanical"
        assert ver[0]["old"] == "1.0.0"
        assert ver[0]["new"] == "1.1.0"

    def test_model_change_is_mechanical(self) -> None:
        deltas = watch.diff({"claude_model": "opus-4-7"}, {"claude_model": "opus-4-8"})
        d = next(x for x in deltas if x["source"] == "claude_model")
        assert d["route"] == "mechanical"

    def test_settings_hash_change_is_mechanical(self) -> None:
        deltas = watch.diff({"settings_hash": "aaaa"}, {"settings_hash": "zzzz"})
        d = next(x for x in deltas if x["source"] == "settings_hash")
        assert d["route"] == "mechanical"

    def test_changelog_change_is_semantic(self) -> None:
        """El changelog es el ÚNICO clasificado semantic (necesita interpretar texto -> PR)."""
        deltas = watch.diff({"changelog_hash": "old"}, {"changelog_hash": "new"})
        d = next(x for x in deltas if x["source"] == "changelog_hash")
        assert d["route"] == "semantic"

    def test_baseline_placeholder_when_old_missing(self) -> None:
        """Primera vez (prev vacío) -> 'old' se reporta como '(baseline)'."""
        deltas = watch.diff({}, {"claude_version": "2.0.0"})
        d = next(x for x in deltas if x["source"] == "claude_version")
        assert d["old"] == "(baseline)"
        assert d["new"] == "2.0.0"

    def test_mixed_changes_split_correctly(self) -> None:
        """Cambio mixto: version+model+settings = mechanical (3); changelog = semantic (1)."""
        prev = {
            "claude_version": "1.0.0",
            "claude_model": "m1",
            "settings_hash": "s1",
            "changelog_hash": "c1",
        }
        cur = {
            "claude_version": "1.0.1",
            "claude_model": "m2",
            "settings_hash": "s2",
            "changelog_hash": "c2",
        }
        deltas = watch.diff(prev, cur)
        mech = [d for d in deltas if d["route"] == "mechanical"]
        sem = [d for d in deltas if d["route"] == "semantic"]
        assert len(deltas) == 4
        assert {d["source"] for d in mech} == {"claude_version", "claude_model", "settings_hash"}
        assert [d["source"] for d in sem] == ["changelog_hash"]

    def test_empty_to_empty_string_is_not_a_delta(self) -> None:
        """Falta una clave en ambos -> "" == "" -> no es delta (no ruido espurio)."""
        # claude_version cambia; las demás faltan en ambos lados (default "")
        deltas = watch.diff({}, {"claude_version": "9.9.9"})
        assert {d["source"] for d in deltas} == {"claude_version"}


# ===========================================================================
# watch_sources.current_state  — subprocess + import de modelo mockeados
# ===========================================================================
class TestCurrentState:
    """current_state: snapshot de las 4 fuentes. subprocess('claude --version') y
    el import de CLAUDE_MODEL se mockean; los hashes se calculan sobre archivos tmp
    reapuntando Path.home()."""

    def _patch_home(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        monkeypatch.setattr(watch.Path, "home", classmethod(lambda cls: home))

    def test_happy_path_collects_all_four_sources(self, tmp_path: Path, monkeypatch) -> None:
        # Estructura ~/.claude/{cache/changelog.md, settings.json}
        home = tmp_path / "home"
        cache = home / ".claude" / "cache"
        cache.mkdir(parents=True)
        (cache / "changelog.md").write_bytes(b"# changelog v1")
        (home / ".claude" / "settings.json").write_bytes(b'{"hooks": {}}')
        self._patch_home(monkeypatch, home)

        # claude --version -> stdout
        completed = mock.Mock(stdout="claude 1.2.3 (build abc)\n")
        monkeypatch.setattr(watch.subprocess, "run", lambda *a, **k: completed)
        # el import dinámico de CLAUDE_MODEL: inyectar módulo falso
        fake_cfg = mock.Mock()
        fake_cfg.CLAUDE_MODEL = "opus-4-8-test"
        monkeypatch.setitem(sys.modules, "engine.v16.config", fake_cfg)

        st = watch.current_state()
        assert st["claude_version"] == "claude 1.2.3 (build abc)"  # stripped
        assert st["claude_model"] == "opus-4-8-test"
        assert len(st["changelog_hash"]) == 16
        assert len(st["settings_hash"]) == 16
        # los dos hashes deben diferir (contenidos distintos)
        assert st["changelog_hash"] != st["settings_hash"]

    def test_claude_cli_missing_fails_open_to_empty_version(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        self._patch_home(monkeypatch, home)

        def boom(*a: object, **k: object) -> None:
            raise FileNotFoundError("claude no instalado")

        monkeypatch.setattr(watch.subprocess, "run", boom)
        fake_cfg = mock.Mock()
        fake_cfg.CLAUDE_MODEL = "m"
        monkeypatch.setitem(sys.modules, "engine.v16.config", fake_cfg)

        st = watch.current_state()
        assert st["claude_version"] == ""  # fail-open, no crash
        # archivos ausentes -> hashes vacíos (también fail-open)
        assert st["changelog_hash"] == ""
        assert st["settings_hash"] == ""

    def test_subprocess_timeout_fails_open(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        self._patch_home(monkeypatch, home)

        def timeout(*a: object, **k: object) -> None:
            raise subprocess.TimeoutExpired(cmd="claude", timeout=10)

        monkeypatch.setattr(watch.subprocess, "run", timeout)
        fake_cfg = mock.Mock()
        fake_cfg.CLAUDE_MODEL = "m"
        monkeypatch.setitem(sys.modules, "engine.v16.config", fake_cfg)

        st = watch.current_state()
        assert st["claude_version"] == ""

    def test_config_import_failure_yields_empty_model(self, tmp_path: Path, monkeypatch) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        self._patch_home(monkeypatch, home)
        monkeypatch.setattr(watch.subprocess, "run", lambda *a, **k: mock.Mock(stdout="v\n"))

        # Forzar que `from engine.v16.config import CLAUDE_MODEL` falle
        real_import = __import__

        def fake_import(name: str, *a: object, **k: object) -> object:
            if name == "engine.v16.config":
                raise ImportError("simulado")
            return real_import(name, *a, **k)  # type: ignore[arg-type]  # variadic pass-through to builtins.__import__; *a typed as object to match monkeypatch signature

        monkeypatch.setattr("builtins.__import__", fake_import)
        st = watch.current_state()
        assert st["claude_model"] == ""  # fail-open


# ===========================================================================
# watch_sources.main  — end-to-end con estado/funciones monkeypatcheadas
# ===========================================================================
class TestWatchMain:
    """main: imprime el reporte JSON, separa mechanical/semantic, y solo escribe
    el baseline cuando se pasa --update. No debe escribir el STATE_FILE real."""

    def _run_main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        prev: dict,
        cur: dict,
        argv: list[str],
    ) -> dict:
        monkeypatch.setattr(watch, "current_state", lambda: cur)
        monkeypatch.setattr(watch, "load_state", lambda: prev)
        monkeypatch.setattr(sys, "argv", argv)
        rc = watch.main()
        out = capsys.readouterr().out
        # El reporte JSON es lo primero impreso (main hace indent=2)
        report = json.loads(out)
        return {"rc": rc, "report": report}

    def test_first_run_flag_and_changed_when_baseline_empty(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(watch, "STATE_FILE", tmp_path / "state.json")
        cur = {
            "claude_version": "1.0.0",
            "claude_model": "m1",
            "settings_hash": "s1",
            "changelog_hash": "c1",
        }
        res = self._run_main(monkeypatch, capsys, prev={}, cur=cur, argv=["watch_sources.py"])
        rep = res["report"]
        assert res["rc"] == 0
        assert rep["first_run"] is True
        assert rep["changed"] is True
        assert rep["current"] == cur
        # sin --update: NO se debe haber escrito el state file
        assert not (tmp_path / "state.json").exists()

    def test_no_change_reports_unchanged(self, tmp_path, monkeypatch, capsys) -> None:
        monkeypatch.setattr(watch, "STATE_FILE", tmp_path / "state.json")
        state = {
            "claude_version": "1.0.0",
            "claude_model": "m1",
            "settings_hash": "s1",
            "changelog_hash": "c1",
        }
        res = self._run_main(
            monkeypatch, capsys, prev=dict(state), cur=dict(state), argv=["watch_sources.py"]
        )
        rep = res["report"]
        assert rep["changed"] is False
        assert rep["first_run"] is False
        assert rep["deltas"] == []
        assert rep["mechanical"] == []
        assert rep["semantic"] == []

    def test_mechanical_and_semantic_buckets_populated(self, tmp_path, monkeypatch, capsys) -> None:
        monkeypatch.setattr(watch, "STATE_FILE", tmp_path / "state.json")
        prev = {
            "claude_version": "1.0.0",
            "claude_model": "m1",
            "settings_hash": "s1",
            "changelog_hash": "c1",
        }
        cur = {
            "claude_version": "1.0.1",  # mechanical
            "claude_model": "m1",  # sin cambio
            "settings_hash": "s1",  # sin cambio
            "changelog_hash": "c2",  # semantic
        }
        res = self._run_main(monkeypatch, capsys, prev=prev, cur=cur, argv=["watch_sources.py"])
        rep = res["report"]
        assert rep["changed"] is True
        assert [d["source"] for d in rep["mechanical"]] == ["claude_version"]
        assert [d["source"] for d in rep["semantic"]] == ["changelog_hash"]

    def test_update_flag_persists_baseline(self, tmp_path, monkeypatch, capsys) -> None:
        state_file = tmp_path / "nested" / "state.json"
        monkeypatch.setattr(watch, "STATE_FILE", state_file)
        cur = {
            "claude_version": "2.0.0",
            "claude_model": "m9",
            "settings_hash": "sx",
            "changelog_hash": "cx",
        }
        res = self._run_main(
            monkeypatch, capsys, prev={}, cur=cur, argv=["watch_sources.py", "--update"]
        )
        assert res["rc"] == 0
        # Con --update se crea el dir padre y se escribe el estado actual
        assert state_file.exists()
        assert json.loads(state_file.read_text()) == cur


# ===========================================================================
# smoke_test.check_mcp_tools
# ===========================================================================
class TestCheckMcpTools:
    """check_mcp_tools: las 5 tools esperadas deben ser callable en el módulo srv.

    check_mcp_tools hace `import integrations.mcp_server as srv`, que se enlaza al
    OBJETO real del submódulo (no a un reemplazo de sys.modules — eso falla cuando
    el módulo ya estaba importado por otra parte de la suite). Por eso parchamos los
    ATRIBUTOS del módulo real vía monkeypatch.setattr, igual que conftest hace con
    SESSIONS_DB. monkeypatch revierte automáticamente al terminar el test.
    """

    def test_all_tools_callable_passes(self, monkeypatch) -> None:
        import integrations.mcp_server as srv

        for t in smoke.EXPECTED_TOOLS:
            monkeypatch.setattr(srv, t, lambda *a, **k: None, raising=False)
        # registry FastMCP introspectable -> N registradas (N = len(EXPECTED_TOOLS))
        fake_mcp = mock.Mock()
        fake_mcp._tool_manager._tools = {t: object() for t in smoke.EXPECTED_TOOLS}
        monkeypatch.setattr(srv, "mcp", fake_mcp, raising=False)

        ok, detail = smoke.check_mcp_tools()
        assert ok is True
        n = len(smoke.EXPECTED_TOOLS)
        assert f"{n}/{n} tools callable" in detail
        assert f"{n} registradas en FastMCP" in detail

    def test_missing_tool_fails(self, monkeypatch) -> None:
        import integrations.mcp_server as srv

        # 4 de las 5 callable
        for t in smoke.EXPECTED_TOOLS[:-1]:
            monkeypatch.setattr(srv, t, lambda *a, **k: None, raising=False)
        # la quinta: atributo NO-callable -> debe reportarse faltante
        monkeypatch.setattr(srv, smoke.EXPECTED_TOOLS[-1], "no-callable", raising=False)
        monkeypatch.setattr(srv, "mcp", None, raising=False)  # introspección se salta sin crash

        ok, detail = smoke.check_mcp_tools()
        assert ok is False
        assert "FALTAN" in detail
        assert smoke.EXPECTED_TOOLS[-1] in detail

    def test_registry_introspection_best_effort_when_absent(self, monkeypatch) -> None:
        """Si no hay _tool_manager (srv.mcp=None), no crashea ni añade el sufijo de registro."""
        import integrations.mcp_server as srv

        for t in smoke.EXPECTED_TOOLS:
            monkeypatch.setattr(srv, t, lambda *a, **k: None, raising=False)
        monkeypatch.setattr(srv, "mcp", None, raising=False)  # getattr(None, ...) -> None

        ok, detail = smoke.check_mcp_tools()
        assert ok is True
        assert "registradas en FastMCP" not in detail


# ===========================================================================
# smoke_test.check_backend
# ===========================================================================
class TestCheckBackend:
    """check_backend: get_stats() debe traer digests/decisions/guards y search() no crashear.

    check_backend hace `from engine.v16 import session_manager as sm`. Parchamos
    get_stats/search en el OBJETO real del módulo (robusto al orden de imports de la
    suite); monkeypatch revierte solo.
    """

    def test_backend_ok_when_stats_complete(self, monkeypatch) -> None:
        from engine.v16 import session_manager as sm

        search_mock = mock.Mock(return_value=[])
        monkeypatch.setattr(
            sm, "get_stats", lambda: {"digests": 1, "decisions": 2, "guards": 3, "extra": 9}
        )
        monkeypatch.setattr(sm, "search", search_mock)

        ok, detail = smoke.check_backend()
        assert ok is True
        assert "sessions.db" in detail
        search_mock.assert_called_once()  # search se ejerció (no debe crashear)

    def test_backend_fails_when_key_missing(self, monkeypatch) -> None:
        from engine.v16 import session_manager as sm

        monkeypatch.setattr(
            sm, "get_stats", lambda: {"digests": 1, "decisions": 2}
        )  # falta 'guards'
        monkeypatch.setattr(sm, "search", lambda *a, **k: [])

        ok, _ = smoke.check_backend()
        assert ok is False

    def test_backend_search_crash_propagates(self, monkeypatch) -> None:
        """Si search crashea, check_backend levanta -> main lo captura como FAIL (fail-closed)."""
        from engine.v16 import session_manager as sm

        def boom(*a: object, **k: object) -> list:
            raise RuntimeError("DB locked")

        monkeypatch.setattr(sm, "get_stats", lambda: {"digests": 1, "decisions": 2, "guards": 3})
        monkeypatch.setattr(sm, "search", boom)

        with pytest.raises(RuntimeError):
            smoke.check_backend()


# ===========================================================================
# smoke_test.check_hooks
# ===========================================================================
class TestCheckHooks:
    """check_hooks: bash -n sobre los .sh; excluye _archive y test_*; FAIL si alguno rompe."""

    def test_all_hooks_parse_passes(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "repo"
        hooks_dir = root / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "a.sh").write_text("#!/usr/bin/env bash\necho ok\n")
        (hooks_dir / "b.sh").write_text("#!/usr/bin/env bash\ntrue\n")
        monkeypatch.setattr(smoke, "ROOT", root)

        ok, detail = smoke.check_hooks()
        assert ok is True
        assert "2/2 hooks cargan" in detail

    def test_broken_hook_fails(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / "repo"
        hooks_dir = root / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "good.sh").write_text("#!/usr/bin/env bash\necho ok\n")
        # sintaxis rota: if sin then/fi
        (hooks_dir / "bad.sh").write_text("#!/usr/bin/env bash\nif [ 1 -eq 1 ]\n")
        monkeypatch.setattr(smoke, "ROOT", root)

        ok, detail = smoke.check_hooks()
        assert ok is False
        assert "bad.sh" in detail
        assert "ROTOS" in detail

    def test_excludes_archive_and_test_prefixed_hooks(self, monkeypatch) -> None:
        # ROOT en un tempdir de nombre NEUTRO: el filtro del módulo es por substring
        # ('"_archive" not in h'), y el nombre de un tmp_path de pytest derivado del
        # nombre del test podría contener 'archive' y excluir TODO espuriamente.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="aris_hooks_") as td:
            root = Path(td)
            hooks_dir = root / "hooks"
            arch = hooks_dir / "_archive"
            arch.mkdir(parents=True)
            # hook válido contado
            (hooks_dir / "live.sh").write_text("#!/usr/bin/env bash\ntrue\n")
            # roto pero en _archive -> excluido
            (arch / "dead.sh").write_text("#!/usr/bin/env bash\nif [ 1\n")
            # roto pero con prefijo test_ -> excluido
            (hooks_dir / "test_fixture.sh").write_text("#!/usr/bin/env bash\nif [ 1\n")
            monkeypatch.setattr(smoke, "ROOT", root)

            ok, detail = smoke.check_hooks()
        assert ok is True
        assert "1/1 hooks cargan" in detail  # solo live.sh contó

    def test_no_hooks_directory_is_vacuously_ok(self, tmp_path, monkeypatch) -> None:
        """Sin .sh -> 0/0 cargan, sin rotos -> pasa (vacuamente)."""
        root = tmp_path / "repo"
        root.mkdir()
        monkeypatch.setattr(smoke, "ROOT", root)
        ok, detail = smoke.check_hooks()
        assert ok is True
        assert "0/0 hooks cargan" in detail


# ===========================================================================
# smoke_test.check_token_counting
# ===========================================================================
class TestCheckTokenCounting:
    """check_token_counting: count_tokens_simple debe devolver int>0; reporta la fuente
    (api si hay ANTHROPIC_API_KEY, si no fallback local).

    Se parchea count_tokens_simple en el OBJETO real de f6_comunicacion (de donde el
    check lo importa), robusto al orden de imports de la suite.
    """

    def test_valid_count_passes_local_fallback(self, monkeypatch) -> None:
        import engine.v16.f6_comunicacion as f6

        monkeypatch.setattr(f6, "count_tokens_simple", lambda *a, **k: 42)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        ok, detail = smoke.check_token_counting()
        assert ok is True
        assert "42 tokens" in detail
        assert "fallback local (sin key)" in detail

    def test_reports_api_source_when_key_present(self, monkeypatch) -> None:
        import engine.v16.f6_comunicacion as f6

        monkeypatch.setattr(f6, "count_tokens_simple", lambda *a, **k: 10)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-dummy")

        ok, detail = smoke.check_token_counting()
        assert ok is True
        assert "api (ANTHROPIC_API_KEY presente)" in detail

    def test_zero_count_fails(self, monkeypatch) -> None:
        import engine.v16.f6_comunicacion as f6

        monkeypatch.setattr(f6, "count_tokens_simple", lambda *a, **k: 0)
        ok, _ = smoke.check_token_counting()
        assert ok is False

    def test_non_int_count_fails(self, monkeypatch) -> None:
        import engine.v16.f6_comunicacion as f6

        monkeypatch.setattr(f6, "count_tokens_simple", lambda *a, **k: "muchos")  # no es int
        ok, _ = smoke.check_token_counting()
        assert ok is False


# ===========================================================================
# smoke_test.main  — agregación + fail-closed
# ===========================================================================
class TestSmokeMain:
    """main: corre los 4 checks; exit 0 sólo si TODOS pasan; cualquier excepción de
    un check = FAIL (fail-closed), nunca propaga."""

    def test_all_checks_pass_exit_zero(self, monkeypatch, capsys) -> None:
        passing = [(name, lambda: (True, "ok")) for name, _ in smoke.CHECKS]
        monkeypatch.setattr(smoke, "CHECKS", passing)
        rc = smoke.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "CONTRATO INTACTO (exit 0)" in out
        assert out.count("[PASS]") == len(passing)

    def test_one_failing_check_exit_one(self, monkeypatch, capsys) -> None:
        checks = [
            ("c1", lambda: (True, "ok")),
            ("c2", lambda: (False, "roto")),
            ("c3", lambda: (True, "ok")),
        ]
        monkeypatch.setattr(smoke, "CHECKS", checks)
        rc = smoke.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "CONTRATO ROTO (exit 1)" in out
        assert "[FAIL] c2: roto" in out

    def test_check_exception_is_failclosed_not_propagated(self, monkeypatch, capsys) -> None:
        def explode() -> tuple[bool, str]:
            raise ValueError("dependencia ausente")

        monkeypatch.setattr(smoke, "CHECKS", [("boom", explode), ("ok", lambda: (True, "fine"))])
        rc = smoke.main()  # NO debe levantar
        out = capsys.readouterr().out
        assert rc == 1  # fail-closed
        assert "[FAIL] boom:" in out
        assert "EXCEPTION: ValueError" in out
        # el otro check sigue corriéndose pese al fallo del primero
        assert "[PASS] ok:" in out

    def test_exception_detail_truncated_to_120_chars(self, monkeypatch, capsys) -> None:
        long_msg = "x" * 500

        def explode() -> tuple[bool, str]:
            raise RuntimeError(long_msg)

        monkeypatch.setattr(smoke, "CHECKS", [("boom", explode)])
        smoke.main()
        out = capsys.readouterr().out
        # el mensaje se recorta a [:120]
        assert ("x" * 120) in out
        assert ("x" * 121) not in out
