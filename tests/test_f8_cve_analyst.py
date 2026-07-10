"""V17 Tramo 1 — cerebro de f8 repunteado a Claude+CVP (veredicto Fable 2026-07-02).

Verifica el enrutamiento de cognición de `_analyze_service` (payload-based, NO vertical-based)
y el helper `_payload_has_phi`. Sin Ollama, sin Claude, sin red — inyecta el cerebro (canned).
El punto crítico: aunque f8 corra en contexto healthcare (ARIS4U_HEALTHCARE=1), un payload
de infra SIN PHI va a Claude+CVP, no al local que alucina CVEs (el trap que Fable cazó).
"""
from __future__ import annotations

import pytest

from engine.v16.f8_assessment import (
    AssessmentScope,
    AssessmentWorkflow,
    TargetType,
    _payload_has_phi,
)


def _scope() -> AssessmentScope:
    return AssessmentScope(
        client_id="client-a",
        target_hosts=["10.0.0.5"],
        target_type=TargetType.CLINIC,
        authorized_techniques=["vuln_scan"],
        contract_ref="SOW-CLIENT-A-2026-07",
    )


# ── _payload_has_phi ─────────────────────────────────────────────────────────────
def test_infra_metadata_is_not_phi() -> None:
    assert _payload_has_phi("10.0.0.5", "nginx", "1.24.0", "nginx") is False
    assert _payload_has_phi("apache", "2.4.58", "httpd", 443) is False


def test_patient_data_is_phi() -> None:
    assert _payload_has_phi("patient record MRN 12345") is True
    assert _payload_has_phi("ssn 123-45-6789") is True
    assert _payload_has_phi("diagnosis: ICD-10 E11.9") is True


# ── _analyze_service: enrutamiento ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_non_phi_uses_injected_claude_brain(monkeypatch) -> None:
    """Payload de infra + cve_analyst inyectado → Claude+CVP, NO local. Aun en healthcare."""
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")  # f8 SIEMPRE corre así — el trap de Fable
    seen = {}

    async def _canned_brain(directive: dict) -> dict:
        seen["directive"] = directive
        return {"cve": "CVE-2024-1234", "cvss_score": 9.1, "severity": "critical",
                "description": "RCE en el servicio", "remediation": "parchear a x.y.z"}

    wf = AssessmentWorkflow(scope=_scope(), cve_analyst=_canned_brain)
    # Si tocara el local, esto explotaría (no hay Ollama en el test).
    out = await wf._analyze_service("10.0.0.5", 443, "nginx", "1.24.0", "nginx")

    assert out["cve"] == "CVE-2024-1234" and out["severity"] == "critical"
    # La directiva lleva headers CVP + contract_ref + prohibición de payloads.
    d = seen["directive"]
    assert d["cvp"]["org"] == "5a188a34"
    assert d["cvp"]["contract_ref"] == "SOW-CLIENT-A-2026-07"
    assert "NO generes exploits ni payloads" in d["constraint"]
    assert d["service"]["host"] == "10.0.0.5"


@pytest.mark.asyncio
async def test_no_brain_no_phi_falls_to_manual_not_local() -> None:
    """Sin cve_analyst y sin PHI → revisión manual honesta, NUNCA fabrica desde el local."""
    called = {"local": False}

    async def _explode(*a, **k):  # el local NO debe llamarse
        called["local"] = True
        raise AssertionError("no debe tocar el local para infra sin PHI")

    wf = AssessmentWorkflow(scope=_scope())  # cve_analyst=None
    wf.llm.query_fast = _explode  # type: ignore[assignment]
    out = await wf._analyze_service("10.0.0.5", 22, "openssh", "9.6", "openssh")

    assert out["cve"] is None
    assert "Manual review required" in out["description"]
    assert called["local"] is False


@pytest.mark.asyncio
async def test_phi_payload_routes_local(monkeypatch) -> None:
    """Payload CON PHI → Ollama local (never-egress), NO el cerebro Claude."""
    brain_called = {"n": 0}

    async def _brain(directive: dict) -> dict:
        brain_called["n"] += 1
        return {"cve": "X"}

    async def _local(prompt: str, system=None) -> str:
        return '{"cve": null, "cvss_score": 0.0, "severity": "info", "description": "local", "remediation": "r"}'

    wf = AssessmentWorkflow(scope=_scope(), cve_analyst=_brain)
    wf.llm.query_fast = _local  # type: ignore[assignment]
    # host con MRN embebido = PHI en el payload
    out = await wf._analyze_service("patient MRN 88231 host", 8080, "hl7-listener", "", "")

    assert out["description"] == "local"       # vino del local PHI-safe
    assert brain_called["n"] == 0              # el cerebro Claude NO se llamó (PHI never-egress)


# ── _cognition (texto: risk summary, exploit-doc, recomendaciones) ───────────────
@pytest.mark.asyncio
async def test_cognition_text_non_phi_uses_brain(monkeypatch) -> None:
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    seen = {}

    async def _brain(directive: dict) -> str:
        seen["d"] = directive
        return "resumen ejecutivo de riesgo"

    wf = AssessmentWorkflow(scope=_scope(), cve_analyst=_brain)
    out = await wf._cognition("risk_summary", "counts: critical=2 high=3", "2", "3",
                              fallback="fb")
    assert out == "resumen ejecutivo de riesgo"
    assert seen["d"]["task"] == "risk_summary"
    assert "NO generes payloads" in seen["d"]["constraint"]
    assert seen["d"]["cvp"]["contract_ref"] == "SOW-CLIENT-A-2026-07"


@pytest.mark.asyncio
async def test_cognition_no_brain_returns_fallback_not_local() -> None:
    async def _explode(*a, **k):
        raise AssertionError("no debe tocar el local para texto de infra sin PHI")

    wf = AssessmentWorkflow(scope=_scope())  # sin cerebro
    wf.llm.query_fast = _explode  # type: ignore[assignment]
    wf.llm.query_quality = _explode  # type: ignore[assignment]
    out = await wf._cognition("recommendations", "top findings infra", fallback="FALLBACK")
    assert out == "FALLBACK"


@pytest.mark.asyncio
async def test_cognition_exploit_doc_carries_system_prompt_and_no_payloads() -> None:
    seen = {}

    async def _brain(directive: dict) -> str:
        seen["d"] = directive
        return '{"technique": "t", "impact": "i", "difficulty": "medium"}'

    wf = AssessmentWorkflow(scope=_scope(), cve_analyst=_brain)
    out = await wf._cognition("exploit_doc", "Target 10.0.0.5 CVE-2024-1", "10.0.0.5",
                              system="Do NOT generate actual exploit code or payloads.")
    assert '"technique"' in out
    assert "Do NOT generate actual exploit code" in seen["d"]["system"]
    assert "NO generes payloads" in seen["d"]["constraint"]
