"""Camino SCOPED de aris_recall_client end-to-end (gap del audit MEDIO).

Los tests existentes solo cubrían presencia/firma (test_v16_7) y la query a nivel
session_manager (test_v2_fixes). Aquí se ejercita la tool completa: ingesta por
cliente → recall scoped → AISLAMIENTO entre clientes + canonicalización de casing.
"""
import pytest

try:
    from integrations.mcp_server import aris_ingest, aris_recall_client
    MCP_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    MCP_AVAILABLE = False

    def aris_ingest(*args, **kwargs):
        return "MCP not available (stub)"

    def aris_recall_client(*args, **kwargs):
        return "MCP not available (stub)"

pytestmark = pytest.mark.skipif(not MCP_AVAILABLE, reason="MCP no instalado")


def test_recall_client_isolates_between_clients() -> None:
    aris_ingest(content="usar bge-m3 como embedder en A",
                content_type="decision", client="ScopeClientA")
    aris_ingest(content="dato confidencial de B",
                content_type="decision", client="ScopeClientB")

    out_a = aris_recall_client("ScopeClientA")
    assert "usar bge-m3 como embedder en A" in out_a
    assert "dato confidencial de B" not in out_a  # aislamiento por cliente


def test_recall_client_canonicalizes_to_lowercase() -> None:
    aris_ingest(content="decision con casing mixto",
                content_type="decision", client="ScopeMixedCase")
    out = aris_recall_client("SCOPEMIXEDCASE")  # entrada en mayúsculas
    assert "=== scopemixedcase Decisions ===" in out
    assert "decision con casing mixto" in out


def test_recall_client_empty_for_unknown_client() -> None:
    out = aris_recall_client("ScopeNobodyHere")
    assert "No decisions for scopenobodyhere" in out
    assert "No guards for scopenobodyhere" in out
