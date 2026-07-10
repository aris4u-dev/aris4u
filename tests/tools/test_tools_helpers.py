"""Tests for two tool helpers: tools/gen_plugin_hooks.py and tools/_logger.py.

Coverage (not refactor) — exercises real behavior:

gen_plugin_hooks.py
    Generates hooks/hooks.json from the ARIS4U hooks in ~/.claude/settings.json,
    rewriting the absolute repo path to ${CLAUDE_PLUGIN_ROOT}. These tests build a
    *controlled* fake settings.json (so they don't depend on the live machine
    config) that mirrors the real 7-event / 7-hook layout, monkeypatch the module
    constants (SETTINGS / OUT / ROOT / ABS_PREFIX) so everything writes to tmp_path,
    and assert: valid JSON, all 7 entries present, ${CLAUDE_PLUGIN_ROOT} substituted,
    zero leftover absolute paths, matcher / timeout preservation, and that non-aris
    hooks and empty groups are dropped.

_logger.py
    emit_event appends a single atomic JSONL line via fcntl.flock and is fail-safe
    (never crashes the caller). These tests cover happy path, JSON validity, append
    semantics, atomicity / no-corruption under repeated writes, the unwritable-dir
    fallback, and non-JSON-native field serialization (default=str).

NOTE: A prior, complementary suite for _logger lives in test_f_series_logger.py;
these tests add bytewise-atomicity + serialization edge cases without overlap that
breaks. All writes are isolated to tmp_path (DEFAULT_LOG / module constants are
monkeypatched), honoring the sacred autouse isolation fixtures in conftest.py.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# tools/ is not a package — add it to sys.path like the sibling logger test does.
_TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import gen_plugin_hooks  # noqa: E402
from _logger import emit_event  # noqa: E402
from datetime import UTC


# ---------------------------------------------------------------------------
# Helpers / fixtures for gen_plugin_hooks
# ---------------------------------------------------------------------------

# The 7 events / 1 aris hook each that mirror the real ~/.claude/settings.json
# layout. ``matcher`` is present only on the two tool-scoped events, matching
# the live config (PreToolUse / PostToolUse), so we exercise both the
# matcher-preservation branch and the matcher-absent branch.
_EVENT_LAYOUT = {
    "UserPromptSubmit": None,
    "Stop": None,
    "SubagentStart": None,
    "PostToolUse": "Bash|Write|Edit|MultiEdit|Agent|Task",
    "SessionStart": None,
    "PreToolUse": "Bash|Write|Edit|MultiEdit|Read|WebFetch|WebSearch",
    "SessionEnd": None,
}


def _make_fake_settings(repo_root: Path) -> dict:
    """Build a settings.json-shaped dict with 7 aris hooks (one per event).

    Each aris command embeds the ``projects/aris4u`` substring (the filter the
    generator uses) and the absolute *repo_root* prefix (so rewrite() has
    something to substitute). One event (PreToolUse) also carries a timeout to
    exercise timeout preservation, plus a *non-aris* foreign hook to prove it
    is filtered out.

    Args:
        repo_root: The fake ARIS4U repo root used as the absolute path prefix.

    Returns:
        A dict shaped like ~/.claude/settings.json.
    """
    py = f"{repo_root}/.venv312/bin/python3"
    hooks: dict = {}
    for event, matcher in _EVENT_LAYOUT.items():
        aris_cmd = f"{py} {repo_root}/hooks/dispatch.py {event}"
        hook_entry: dict = {"type": "command", "command": aris_cmd}
        if event == "PreToolUse":
            hook_entry["timeout"] = 10
        group: dict = {"hooks": [hook_entry]}
        if matcher is not None:
            group["matcher"] = matcher
        hooks[event] = [group]

    # A foreign (non-aris) hook in its own event that must be dropped entirely.
    hooks["Notification"] = [
        {"hooks": [{"type": "command", "command": "/usr/bin/say done"}]}
    ]
    # A foreign hook *mixed into* an existing aris event group must also be
    # dropped, while the aris hook in the same event survives.
    hooks["Stop"][0]["hooks"].append(
        {"type": "command", "command": "/opt/other-tool/run.sh"}
    )
    return {"hooks": hooks}


@pytest.fixture
def gen_env(tmp_path, monkeypatch):
    """Wire gen_plugin_hooks to a hermetic tmp repo + fake settings.

    Patches the module-level constants so main() reads our fake settings and
    writes hooks.json under tmp_path. Returns (repo_root, settings_path,
    out_path) for assertions.
    """
    # The generator filters hooks by the literal substring "projects/aris4u"
    # in each command, so the fake repo root must contain that path segment.
    repo_root = tmp_path / "projects" / "aris4u"
    (repo_root / "hooks").mkdir(parents=True)

    settings_path = tmp_path / "settings.json"
    out_path = repo_root / "hooks" / "hooks.json"

    settings_path.write_text(json.dumps(_make_fake_settings(repo_root)))

    monkeypatch.setattr(gen_plugin_hooks, "ROOT", repo_root)
    monkeypatch.setattr(gen_plugin_hooks, "SETTINGS", settings_path)
    monkeypatch.setattr(gen_plugin_hooks, "OUT", out_path)
    monkeypatch.setattr(gen_plugin_hooks, "ABS_PREFIX", str(repo_root))

    return repo_root, settings_path, out_path


# ---------------------------------------------------------------------------
# gen_plugin_hooks.main()
# ---------------------------------------------------------------------------


class TestGenPluginHooksMain:
    """End-to-end behavior of the hooks.json generator."""

    def test_returns_zero_and_writes_file(self, gen_env):
        """main() succeeds (exit 0) and writes hooks.json to tmp_path."""
        _repo, _settings, out_path = gen_env
        rc = gen_plugin_hooks.main()
        assert rc == 0
        assert out_path.exists(), "hooks.json was not written"

    def test_output_is_valid_json_with_hooks_root(self, gen_env):
        """Output parses as JSON with a top-level 'hooks' object."""
        _repo, _settings, out_path = gen_env
        gen_plugin_hooks.main()
        data = json.loads(out_path.read_text())
        assert isinstance(data, dict)
        assert "hooks" in data
        assert isinstance(data["hooks"], dict)

    def test_seven_events_and_seven_hooks(self, gen_env):
        """All 7 aris events survive and total aris hook count is 7."""
        _repo, _settings, out_path = gen_env
        gen_plugin_hooks.main()
        ev = json.loads(out_path.read_text())["hooks"]

        assert set(ev.keys()) == set(_EVENT_LAYOUT.keys())
        assert len(ev) == 7

        total = sum(len(g["hooks"]) for groups in ev.values() for g in groups)
        assert total == 7, f"expected 7 aris hooks, got {total}"

    def test_claude_plugin_root_substituted(self, gen_env):
        """Absolute repo prefix becomes ${CLAUDE_PLUGIN_ROOT} in every command."""
        repo_root, _settings, out_path = gen_env
        gen_plugin_hooks.main()
        text = out_path.read_text()

        assert "${CLAUDE_PLUGIN_ROOT}" in text
        # Each command carries the prefix twice (interpreter path + script path),
        # and rewrite() replaces ALL occurrences: 2 per command x 7 commands.
        assert text.count("${CLAUDE_PLUGIN_ROOT}") == 14

        ev = json.loads(text)["hooks"]
        for groups in ev.values():
            for g in groups:
                for h in g["hooks"]:
                    assert h["command"].startswith("${CLAUDE_PLUGIN_ROOT}") or \
                        "${CLAUDE_PLUGIN_ROOT}" in h["command"]
                    assert str(repo_root) not in h["command"]

    def test_no_leftover_absolute_paths(self, gen_env):
        """The sanity invariant: zero absolute-prefix occurrences remain."""
        repo_root, _settings, out_path = gen_env
        gen_plugin_hooks.main()
        assert out_path.read_text().count(str(repo_root)) == 0

    def test_matcher_preserved_only_where_present(self, gen_env):
        """matcher is kept for tool-scoped events, absent otherwise."""
        _repo, _settings, out_path = gen_env
        gen_plugin_hooks.main()
        ev = json.loads(out_path.read_text())["hooks"]

        assert ev["PreToolUse"][0]["matcher"] == _EVENT_LAYOUT["PreToolUse"]
        assert ev["PostToolUse"][0]["matcher"] == _EVENT_LAYOUT["PostToolUse"]
        # Events with matcher=None must NOT have a matcher key emitted.
        assert "matcher" not in ev["UserPromptSubmit"][0]
        assert "matcher" not in ev["SessionStart"][0]

    def test_timeout_preserved_when_present(self, gen_env):
        """timeout is copied through only for the hook that declared it."""
        _repo, _settings, out_path = gen_env
        gen_plugin_hooks.main()
        ev = json.loads(out_path.read_text())["hooks"]

        pre_hook = ev["PreToolUse"][0]["hooks"][0]
        assert pre_hook["timeout"] == 10
        # A hook without a declared timeout must omit the key.
        other_hook = ev["SessionStart"][0]["hooks"][0]
        assert "timeout" not in other_hook

    def test_hook_entry_shape(self, gen_env):
        """Every emitted hook is a command entry with type+command."""
        _repo, _settings, out_path = gen_env
        gen_plugin_hooks.main()
        ev = json.loads(out_path.read_text())["hooks"]
        for groups in ev.values():
            for g in groups:
                for h in g["hooks"]:
                    assert h["type"] == "command"
                    assert isinstance(h["command"], str) and h["command"]

    def test_foreign_hooks_dropped(self, gen_env):
        """Non-aris events are dropped and mixed foreign hooks are filtered."""
        _repo, _settings, out_path = gen_env
        gen_plugin_hooks.main()
        ev = json.loads(out_path.read_text())["hooks"]

        # The wholly-foreign event must not appear.
        assert "Notification" not in ev
        # The Stop event keeps ONLY its single aris hook (foreign sibling dropped).
        stop_hooks = ev["Stop"][0]["hooks"]
        assert len(stop_hooks) == 1
        assert "projects/aris4u" in stop_hooks[0]["command"] or \
            "${CLAUDE_PLUGIN_ROOT}" in stop_hooks[0]["command"]
        assert "other-tool" not in json.dumps(ev)

    def test_output_ends_with_newline(self, gen_env):
        """File ends with a trailing newline (POSIX-friendly diff)."""
        _repo, _settings, out_path = gen_env
        gen_plugin_hooks.main()
        assert out_path.read_text().endswith("\n")

    def test_empty_settings_yields_empty_hooks(self, gen_env):
        """No aris hooks anywhere -> empty hooks object, still exit 0."""
        _repo, settings_path, out_path = gen_env
        settings_path.write_text(json.dumps({"hooks": {}}))
        rc = gen_plugin_hooks.main()
        assert rc == 0
        assert json.loads(out_path.read_text())["hooks"] == {}

    def test_settings_without_hooks_key(self, gen_env):
        """settings.json lacking a 'hooks' key is tolerated (uses .get default)."""
        _repo, settings_path, out_path = gen_env
        settings_path.write_text(json.dumps({"other": 1}))
        rc = gen_plugin_hooks.main()
        assert rc == 0
        assert json.loads(out_path.read_text())["hooks"] == {}


class TestGenPluginHooksRewrite:
    """Unit behavior of the pure rewrite() helper."""

    def test_rewrite_substitutes_prefix(self, monkeypatch):
        """rewrite() swaps the configured ABS_PREFIX for the plugin var."""
        monkeypatch.setattr(gen_plugin_hooks, "ABS_PREFIX", "/abs/repo")
        out = gen_plugin_hooks.rewrite("/abs/repo/hooks/dispatch.py PreToolUse")
        assert out == "${CLAUDE_PLUGIN_ROOT}/hooks/dispatch.py PreToolUse"

    def test_rewrite_noop_without_prefix(self, monkeypatch):
        """A command without the prefix is returned unchanged."""
        monkeypatch.setattr(gen_plugin_hooks, "ABS_PREFIX", "/abs/repo")
        cmd = "/usr/bin/python3 /elsewhere/run.py"
        assert gen_plugin_hooks.rewrite(cmd) == cmd

    def test_rewrite_all_occurrences(self, monkeypatch):
        """Every occurrence of the prefix is replaced, not just the first."""
        monkeypatch.setattr(gen_plugin_hooks, "ABS_PREFIX", "/p")
        out = gen_plugin_hooks.rewrite("/p/a && /p/b")
        assert out == "${CLAUDE_PLUGIN_ROOT}/a && ${CLAUDE_PLUGIN_ROOT}/b"


class TestGenPluginHooksRootResolution:
    """ROOT honors the ARIS4U_ROOT env override at import time."""

    def test_root_env_override(self, tmp_path, monkeypatch):
        """Re-importing with ARIS4U_ROOT set pins ROOT to that path."""
        monkeypatch.setenv("ARIS4U_ROOT", str(tmp_path))
        mod = importlib.reload(gen_plugin_hooks)
        try:
            assert mod.ROOT == tmp_path
            assert mod.ABS_PREFIX == str(tmp_path)
            assert mod.OUT == tmp_path / "hooks" / "hooks.json"
        finally:
            # Restore the module to its env-clean state for other tests.
            monkeypatch.delenv("ARIS4U_ROOT", raising=False)
            importlib.reload(gen_plugin_hooks)


# ---------------------------------------------------------------------------
# _logger.emit_event()
# ---------------------------------------------------------------------------


class TestEmitEventHappyPath:
    """emit_event writes a single, valid, atomic JSONL record."""

    def test_writes_single_valid_json_line(self, tmp_path):
        """One call -> exactly one parseable JSON line with core fields."""
        log_file = tmp_path / "events.jsonl"
        with patch("_logger.DEFAULT_LOG", log_file):
            emit_event("plugin_gen", "gen_plugin_hooks", hooks=7, events=7)

        assert log_file.exists()
        lines = log_file.read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "plugin_gen"
        assert rec["source"] == "gen_plugin_hooks"
        assert rec["hooks"] == 7
        assert rec["events"] == 7
        # Timestamp present and ISO-8601 with tz (parseable).
        assert "ts" in rec
        from datetime import datetime

        datetime.fromisoformat(rec["ts"])

    def test_appends_not_truncates(self, tmp_path):
        """Successive calls append; earlier records are preserved."""
        log_file = tmp_path / "events.jsonl"
        with patch("_logger.DEFAULT_LOG", log_file):
            emit_event("a", "src", n=1)
            emit_event("b", "src", n=2)
            emit_event("c", "src", n=3)

        lines = log_file.read_text().splitlines()
        assert [json.loads(line)["event"] for line in lines] == ["a", "b", "c"]
        assert [json.loads(line)["n"] for line in lines] == [1, 2, 3]

    def test_each_line_terminated_by_newline(self, tmp_path):
        """Each record ends in '\\n' so the file is true line-delimited JSON."""
        log_file = tmp_path / "events.jsonl"
        with patch("_logger.DEFAULT_LOG", log_file):
            emit_event("x", "src")
            emit_event("y", "src")
        raw = log_file.read_text()
        assert raw.endswith("\n")
        assert raw.count("\n") == 2


class TestEmitEventAtomicity:
    """Repeated writes never corrupt or interleave a record."""

    def test_no_corruption_under_many_writes(self, tmp_path):
        """200 sequential locked appends each remain individually valid JSON."""
        log_file = tmp_path / "events.jsonl"
        with patch("_logger.DEFAULT_LOG", log_file):
            for i in range(200):
                emit_event("evt", "src", index=i, payload="x" * 64)

        lines = log_file.read_text().splitlines()
        assert len(lines) == 200
        for i, line in enumerate(lines):
            rec = json.loads(line)  # raises if a record was torn
            assert rec["index"] == i
            assert rec["payload"] == "x" * 64

    def test_full_record_written_as_one_unit(self, tmp_path):
        """The complete record (with all fields) lands in one line, never split."""
        log_file = tmp_path / "events.jsonl"
        with patch("_logger.DEFAULT_LOG", log_file):
            emit_event("e", "s", a=1, b=2, c=3, nested={"k": [1, 2, 3]})
        lines = log_file.read_text().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["a"] == 1 and rec["b"] == 2 and rec["c"] == 3
        assert rec["nested"] == {"k": [1, 2, 3]}


class TestEmitEventFailSafe:
    """Instrumentation must never crash the caller."""

    def test_unwritable_parent_does_not_crash_but_creates_dir(self, tmp_path):
        """A missing parent dir is created (mkdir parents=True), no exception."""
        log_file = tmp_path / "deep" / "nested" / "events.jsonl"
        with patch("_logger.DEFAULT_LOG", log_file):
            emit_event("e", "s")  # must not raise
        assert log_file.exists()

    def test_oserror_swallowed(self, tmp_path):
        """If the open() itself raises OSError, emit_event silently no-ops."""
        log_file = tmp_path / "events.jsonl"
        with patch("_logger.DEFAULT_LOG", log_file):
            with patch("builtins.open", side_effect=OSError("disk full")):
                # Must not propagate — best-effort instrumentation.
                emit_event("e", "s")
        # Nothing written, but caller survived.
        assert not log_file.exists()

    def test_mkdir_failure_swallowed(self, tmp_path):
        """A mkdir OSError is swallowed too (never crash the caller)."""
        log_file = tmp_path / "events.jsonl"
        with patch("_logger.DEFAULT_LOG", log_file):
            with patch.object(Path, "mkdir", side_effect=OSError("ro fs")):
                emit_event("e", "s")  # must not raise
        assert not log_file.exists()


class TestEmitEventSerialization:
    """default=str keeps non-JSON-native fields from crashing the writer."""

    def test_non_json_native_field_serialized_via_str(self, tmp_path):
        """A Path value (not JSON-native) is coerced via default=str, not crash."""
        log_file = tmp_path / "events.jsonl"
        weird = Path("/tmp/some/path")
        with patch("_logger.DEFAULT_LOG", log_file):
            emit_event("e", "s", where=weird)
        rec = json.loads(log_file.read_text().splitlines()[0])
        assert rec["where"] == str(weird)

    def test_datetime_field_serialized(self, tmp_path):
        """A datetime value is stringified rather than raising TypeError."""
        from datetime import datetime

        log_file = tmp_path / "events.jsonl"
        when = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
        with patch("_logger.DEFAULT_LOG", log_file):
            emit_event("e", "s", when=when)
        rec = json.loads(log_file.read_text().splitlines()[0])
        assert rec["when"] == str(when)

    def test_no_extra_fields_returns_none(self, tmp_path):
        """emit_event returns None and writes hash-chain fields in every record.

        Batch F2: every event now carries ``prev_hash`` and ``hash`` (SHA-256
        append-only chain for EU AI Act Art.12 tamper evidence).  The chain
        starts at GENESIS_HASH when no prior chain head exists.
        """
        log_file = tmp_path / "events.jsonl"
        with patch("_logger.DEFAULT_LOG", log_file):
            ret = emit_event("bare", "src")
        assert ret is None
        rec = json.loads(log_file.read_text().splitlines()[0])
        # Core fields always present
        assert {"ts", "event", "source"}.issubset(rec.keys())
        # Hash-chain fields always present (Batch F2)
        assert "prev_hash" in rec
        assert "hash" in rec
        assert len(rec["prev_hash"]) == 64
        assert len(rec["hash"]) == 64
        # First event in a fresh chain: prev_hash is genesis (all zeros)
        assert rec["prev_hash"] == "0" * 64


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
