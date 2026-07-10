"""Tests de mcp_guard — telemetría + (solo modo healthcare) deny de PHI→egress.

Por defecto SILENCIOSO (no estorba trabajo legítimo, incl. QuickBooks de clientes).
``ARIS4U_ROOT`` se monkeypatchea a tmp_path para que la telemetría no toque el log real.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from dispatch.handlers import mcp_guard as mg  # noqa: E402
from dispatch.handlers import verdict as V  # noqa: E402

_CLIENT_C = "/Users/x/projects/client-c/inventory-system"


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(mg, "ARIS4U_ROOT", tmp_path)
    monkeypatch.delenv("ARIS4U_MCP_ALLOW", raising=False)
    monkeypatch.delenv("ARIS4U_HEALTHCARE", raising=False)
    # CI hermeticity: _HEALTHCARE_PATH_MARKERS is computed at import time from config.
    # Inject the canonical client-c markers so CWD-based detection works without config.
    monkeypatch.setattr(mg._phi, "_HEALTHCARE_PATH_MARKERS", ("client-c", "/client-c/"))
    return tmp_path


def _log_lines(tmp: Path) -> list[dict]:
    f = tmp / "logs" / "v16.1-events.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


# --------------------------------------------------------------------------- #
# Parsing + passthrough
# --------------------------------------------------------------------------- #
def test_parse_valid() -> None:
    assert mg._parse("mcp__supabase__execute_sql") == ("supabase", "execute_sql")


def test_parse_non_mcp_is_none() -> None:
    assert mg._parse("Bash") is None
    assert mg._parse("mcp__sinsep") is None


def test_non_mcp_passes_through() -> None:
    assert mg.check("Bash", {}).kind == V.PASS


# --------------------------------------------------------------------------- #
# SILENCIOSO por defecto — no estorba trabajo legítimo
# --------------------------------------------------------------------------- #
def test_quickbooks_not_blocked() -> None:
    # El usuario maneja QuickBooks de clientes: jamás se bloquea ni se avisa.
    for tool in ("qbo_sales_create_invoice", "qbo_payroll_get_employees", "quickbooks_transaction_import"):
        assert mg.check(f"mcp__claude_ai_Intuit_QuickBooks__{tool}", {}).kind == V.PASS


def test_normal_mcp_calls_are_silent() -> None:
    # supabase write, egress, FMP, etc. fuera de healthcare → PASS (solo telemetría).
    for name in (
        "mcp__supabase__execute_sql",
        "mcp__supabase__delete_branch",
        "mcp__claude_ai_Google_Drive__create_file",
        "mcp__atlassian__createJiraIssue",
        "mcp__claude_ai_FMP__quote",
    ):
        assert mg.check(name, {}, cwd="/Users/x/projects/lab-project-1").kind == V.PASS


# --------------------------------------------------------------------------- #
# Telemetría siempre
# --------------------------------------------------------------------------- #
def test_logs_every_mcp_call(_env: Path) -> None:
    mg.check("mcp__supabase__list_tables", {})
    mg.check("mcp__claude_ai_FMP__quote", {})
    events = _log_lines(_env)
    assert len(events) == 2
    assert {e["event"] for e in events} == {"mcp_call"}


# --------------------------------------------------------------------------- #
# PHI→egress: deny SOLO en modo healthcare (opt-in)
# --------------------------------------------------------------------------- #
def test_phi_egress_denied_in_healthcare() -> None:
    v = mg.check("mcp__claude_ai_Google_Drive__create_file",
                 {"content": "patient SSN 123-45-6789"}, cwd=_CLIENT_C)
    assert v.kind == V.DENY
    assert "PHI" in v.text


def test_phi_egress_passes_outside_healthcare() -> None:
    # Mismo PHI pero fuera de modo healthcare → PASS (no estorba).
    v = mg.check("mcp__claude_ai_Google_Drive__create_file",
                 {"content": "patient SSN 123-45-6789"}, cwd="/tmp/proj")
    assert v.kind == V.PASS


def test_phi_egress_override_downgrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_MCP_ALLOW", "1")
    v = mg.check("mcp__claude_ai_Google_Drive__create_file",
                 {"content": "patient SSN 123-45-6789"}, cwd=_CLIENT_C)
    assert v.kind == V.ADVISE


def test_phi_in_supabase_not_denied_even_in_healthcare() -> None:
    # supabase NO es egress externo (es el store del cliente) → no deny aunque haya PHI.
    v = mg.check("mcp__supabase__execute_sql",
                 {"query": "insert ssn 123-45-6789"}, cwd=_CLIENT_C)
    assert v.kind == V.PASS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
