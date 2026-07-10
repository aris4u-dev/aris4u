"""V16.9: Public-API tests for the 4-tool MCP server (post-shrink).

Replaces tests/test_mcp_server.py (skipped at module level since V16.5.2)
and updates the original 7-tool surface tested in V16.7.

V16.9 SHRINK rationale (3 tools removed):
- aris_recall:   sqlite schema drift (no such column: embedding); convergence with claude-mem
- aris_ask:      Claude bridge timed out 120s; convergence with native Agent dispatch
- aris_dispatch: target model 'mac_coder' missing; convergence with Agent run_in_background

Scope: signature contracts + non-crashing invocation. We do NOT exercise
DB/network paths here.
"""
from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest

# Skip all tests in this module if 'mcp' package is not installed
mcp = pytest.importorskip("mcp", reason="mcp package not installed — skip MCP tests")

from integrations import mcp_server


EXPECTED_TOOLS: dict[str, tuple[list[str], str]] = {
    # tool_name: (required_params, return_annotation)
    "aris_ingest":    (["content"],                            "str"),
    "aris_search":    (["query"],                              "str"),
    "aris_dialectic": (["task"],                               "str"),
    "aris_structure": (["idea"],                               "str"),  # F1 PRE
    "aris_critique":  (["response"],                           "str"),  # F1 POST
    "aris_health":    ([],                                     "str"),
}


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS.keys()))
def test_tool_exists_on_module(tool_name):
    """Each of the 7 documented MCP tools must be importable from
    `integrations.mcp_server` at module scope."""
    assert hasattr(mcp_server, tool_name), (
        f"MCP tool {tool_name} missing from public surface"
    )
    fn = getattr(mcp_server, tool_name)
    assert callable(fn), f"{tool_name} is not callable"


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS.keys()))
def test_tool_signature_required_params(tool_name):
    """Required parameters (positional, no default) must match the doc."""
    fn = getattr(mcp_server, tool_name)
    sig = inspect.signature(fn)
    required = [
        p.name
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
        and p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        )
    ]
    expected_required, _ = EXPECTED_TOOLS[tool_name]
    assert required == expected_required, (
        f"{tool_name} required params drifted: expected {expected_required}, got {required}"
    )


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOLS.keys()))
def test_tool_returns_str(tool_name):
    """All 7 MCP tools must declare `-> str` return annotation. The MCP
    protocol expects string responses; loosening this contract would break
    Claude Code's tool-use rendering."""
    fn = getattr(mcp_server, tool_name)
    hints = get_type_hints(fn)
    return_type = hints.get("return")
    assert return_type is str, (
        f"{tool_name} return annotation is {return_type}, expected str"
    )


def test_four_tools_exactly():
    """V16.9+: exactly 7 MCP tools (WS4 añadió aris_recall_client; F1 2026-06-19 añadió
    aris_structure + aris_critique) — añadir/quitar exige actualizar este test Y el CHANGELOG."""
    tools = [name for name in dir(mcp_server) if name.startswith("aris_")]
    public_tools = [t for t in tools if not t.startswith("_")]
    assert len(public_tools) == 7, (
        f"Expected 7 MCP tools, found {len(public_tools)}: {public_tools}"
    )
    # Verify the expected tools are present
    expected = {
        "aris_dialectic", "aris_health", "aris_ingest", "aris_search",
        "aris_recall_client", "aris_structure", "aris_critique",
    }
    assert set(public_tools) == expected, (
        f"Expected tools {expected}, but got {set(public_tools)}"
    )


def test_aris_health_no_required_args():
    """aris_health must work without arguments — it's the one tool Claude
    is expected to call without context (status check)."""
    fn = mcp_server.aris_health
    sig = inspect.signature(fn)
    required = [
        p.name
        for p in sig.parameters.values()
        if p.default is inspect.Parameter.empty
    ]
    assert required == [], f"aris_health should accept zero required args, got {required}"


def test_aris_ingest_optional_metadata():
    """aris_ingest must accept content_type, domain, rationale as optional
    kwargs — pinning this protects callers that pass them by name."""
    fn = mcp_server.aris_ingest
    sig = inspect.signature(fn)
    optional = {
        p.name: p.default
        for p in sig.parameters.values()
        if p.default is not inspect.Parameter.empty
    }
    assert "content_type" in optional
    assert "domain" in optional
    assert "rationale" in optional
    # Pin defaults to catch silent contract drift
    assert optional["content_type"] == "decision"
    assert optional["domain"] == ""
    assert optional["rationale"] == ""


