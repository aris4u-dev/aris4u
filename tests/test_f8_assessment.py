"""Tests for F8.ASSESSMENT — Healthcare pentest workflow orchestrator.

Test coverage:
- LLMRouter: fast/quality model selection, availability check
- HITLGate: checkpoint CRUD and approval workflow
- AssessmentWorkflow: scope validation, session lifecycle
- Scope enforcement: exact IP matching, CIDR validation
- HITL gate protection: blocks exploitation without approval
- Recon phase: nmap integration and scope validation
- Scan phase: LLM-based CVE analysis
- Report generation: bilingual markdown reports
- Session persistence: database serialization
"""

import asyncio
import json
import sqlite3
import pytest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from engine.v16.f8_assessment import (
    AssessmentPhase,
    AssessmentScope,
    AssessmentSession,
    AssessmentWorkflow,
    HITLCheckpoint,
    HITLGate,
    HITLStatus,
    LLMRouter,
    ReconResult,
    ScanResult,
    TargetType,
    VulnFinding,
)


def make_scope() -> AssessmentScope:
    """Helper to create a test assessment scope.

    Returns:
        AssessmentScope for testing
    """
    return AssessmentScope(
        client_id="test-client",
        target_hosts=["192.168.1.1"],
        target_type=TargetType.CLINIC,
        authorized_techniques=["port_scan", "vuln_scan"],
        contract_ref="CONTRACT-TEST-001",
    )


class TestLLMRouter:
    """LLMRouter model selection and availability tests."""

    @pytest.mark.asyncio
    async def test_llm_router_query_fast_success(self) -> None:
        """Fast model query returns response from qwen35-analyst."""
        router = LLMRouter(ollama_url="http://localhost:11434")

        with patch("httpx.AsyncClient.post") as mock_post:
            mock_response = MagicMock()
            mock_response.json.return_value = {"response": "Fast analysis result"}
            mock_response.raise_for_status = MagicMock()
            mock_post.return_value.__aenter__.return_value.post.return_value = (
                mock_response
            )

            with patch(
                "httpx.AsyncClient.__aenter__"
            ) as mock_enter, patch(
                "httpx.AsyncClient.__aexit__"
            ) as mock_exit:
                mock_client = MagicMock()
                mock_client.post = AsyncMock(
                    return_value=MagicMock(
                        json=MagicMock(
                            return_value={"response": "Fast analysis result"}
                        ),
                        raise_for_status=MagicMock(),
                    )
                )
                mock_enter.return_value = mock_client
                mock_exit.return_value = None

                # Simpler approach: directly call with mocked AsyncClient
                import httpx

                async def mock_post_call(*args, **kwargs):
                    return MagicMock(
                        json=MagicMock(
                            return_value={"response": "Fast analysis result"}
                        ),
                        raise_for_status=MagicMock(),
                    )

                with patch.object(
                    httpx.AsyncClient, "post", new_callable=AsyncMock
                ) as mock_async_post:
                    mock_async_post.return_value = MagicMock(
                        json=lambda: {"response": "Fast analysis result"},
                        raise_for_status=lambda: None,
                    )

                    result = await router.query_fast("test prompt")
                    assert result == "Fast analysis result"

    @pytest.mark.asyncio
    async def test_llm_router_query_quality_success(self) -> None:
        """Quality model query returns response from hermes3:70b."""
        router = LLMRouter(ollama_url="http://localhost:11434")

        import httpx

        with patch.object(
            httpx.AsyncClient, "post", new_callable=AsyncMock
        ) as mock_post:
            mock_post.return_value = MagicMock(
                json=lambda: {"response": "Quality report text"},
                raise_for_status=lambda: None,
            )

            result = await router.query_quality("test prompt")
            assert result == "Quality report text"

    @pytest.mark.asyncio
    async def test_llm_router_is_available_true(self) -> None:
        """Ollama availability check returns True when reachable."""
        router = LLMRouter(ollama_url="http://localhost:11434")

        import httpx

        with patch.object(httpx.AsyncClient, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(status_code=200)

            result = await router.is_available()
            assert result is True

    @pytest.mark.asyncio
    async def test_llm_router_is_available_false(self) -> None:
        """Ollama availability check returns False on connection error."""
        router = LLMRouter(ollama_url="http://localhost:11434")

        import httpx

        with patch.object(
            httpx.AsyncClient, "get", new_callable=AsyncMock
        ) as mock_get:
            mock_get.side_effect = httpx.ConnectError("Connection failed")

            result = await router.is_available()
            assert result is False


class TestHITLGate:
    """HITLGate checkpoint management tests."""

    def test_hitl_create_checkpoint(self, tmp_path: Path) -> None:
        """Create checkpoint persists to database with PENDING status."""
        db_path = tmp_path / "test_sessions.db"
        gate = HITLGate(db_path=db_path)

        checkpoint = gate.create_checkpoint(
            session_id="test_session_123",
            phase=AssessmentPhase.EXPLOIT,
            context={"finding_count": 5, "critical": 2},
        )

        assert checkpoint.status == HITLStatus.PENDING
        assert checkpoint.phase == AssessmentPhase.EXPLOIT
        assert checkpoint.context["finding_count"] == 5
        assert checkpoint.resolver is None

    def test_hitl_approve_checkpoint(self, tmp_path: Path) -> None:
        """Approve checkpoint updates status to APPROVED."""
        db_path = tmp_path / "test_sessions.db"
        gate = HITLGate(db_path=db_path)

        checkpoint = gate.create_checkpoint(
            session_id="test_session_123",
            phase=AssessmentPhase.EXPLOIT,
            context={"finding_count": 5},
        )

        approved = gate.approve(checkpoint.checkpoint_id, resolver="analyst@clinic.com")

        assert approved.status == HITLStatus.APPROVED
        assert approved.resolver == "analyst@clinic.com"
        assert approved.resolved_at is not None

    def test_hitl_reject_checkpoint(self, tmp_path: Path) -> None:
        """Reject checkpoint updates status to REJECTED."""
        db_path = tmp_path / "test_sessions.db"
        gate = HITLGate(db_path=db_path)

        checkpoint = gate.create_checkpoint(
            session_id="test_session_123",
            phase=AssessmentPhase.EXPLOIT,
            context={"finding_count": 5},
        )

        rejected = gate.reject(
            checkpoint.checkpoint_id,
            resolver="ciso@clinic.com",
            reason="Waiting for patch",
        )

        assert rejected.status == HITLStatus.REJECTED
        assert rejected.resolver == "ciso@clinic.com"

    def test_hitl_get_checkpoint(self, tmp_path: Path) -> None:
        """Get checkpoint retrieves stored checkpoint from database."""
        db_path = tmp_path / "test_sessions.db"
        gate = HITLGate(db_path=db_path)

        created = gate.create_checkpoint(
            session_id="test_session_123",
            phase=AssessmentPhase.EXPLOIT,
            context={"finding_count": 5, "critical": 2},
        )

        retrieved = gate.get_checkpoint(created.checkpoint_id)

        assert retrieved is not None
        assert retrieved.checkpoint_id == created.checkpoint_id
        assert retrieved.status == HITLStatus.PENDING
        assert retrieved.context["critical"] == 2

    def test_hitl_get_nonexistent(self, tmp_path: Path) -> None:
        """Get nonexistent checkpoint returns None."""
        db_path = tmp_path / "test_sessions.db"
        gate = HITLGate(db_path=db_path)

        result = gate.get_checkpoint("nonexistent_checkpoint_id")

        assert result is None

    def test_hitl_approve_nonexistent_raises(self, tmp_path: Path) -> None:
        """Approve nonexistent checkpoint raises ValueError."""
        db_path = tmp_path / "test_sessions.db"
        gate = HITLGate(db_path=db_path)

        with pytest.raises(ValueError, match="not found"):
            gate.approve("nonexistent_id", resolver="user@clinic.com")


class TestAssessmentWorkflow:
    """AssessmentWorkflow scope validation and session lifecycle."""

    def test_validate_scope_exact_host(self) -> None:
        """Validate scope accepts exact host match."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.1"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        workflow = AssessmentWorkflow(scope=scope)

        assert workflow._validate_scope("192.168.1.1") is True

    def test_validate_scope_cidr_match(self) -> None:
        """Validate scope accepts CIDR range matches."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.0/24"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        workflow = AssessmentWorkflow(scope=scope)

        assert workflow._validate_scope("192.168.1.100") is True
        assert workflow._validate_scope("192.168.1.255") is True

    def test_validate_scope_out_of_range(self) -> None:
        """Validate scope rejects IP outside authorized range."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.0/24"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        workflow = AssessmentWorkflow(scope=scope)

        assert workflow._validate_scope("10.0.0.1") is False
        assert workflow._validate_scope("192.168.2.1") is False

    def test_validate_scope_hostname_match(self) -> None:
        """Validate scope accepts exact hostname match."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["clinic-main.local", "clinic-backup.local"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        workflow = AssessmentWorkflow(scope=scope)

        assert workflow._validate_scope("clinic-main.local") is True
        assert workflow._validate_scope("clinic-other.local") is False

    @pytest.mark.asyncio
    async def test_assessment_scope_creation(self) -> None:
        """AssessmentScope dataclass creates engagement authorization."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.0/24"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan", "vuln_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )

        assert scope.client_id == "clinic_001"
        assert len(scope.target_hosts) == 1
        assert scope.target_type == TargetType.CLINIC
        assert len(scope.authorized_techniques) == 2
        assert scope.engagement_id is not None

    @pytest.mark.asyncio
    async def test_assessment_session_dataclass(self) -> None:
        """AssessmentSession dataclass initializes correctly."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.0/24"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        session = AssessmentSession(
            session_id="sess_abc123",
            scope=scope,
            current_phase=AssessmentPhase.RECON,
        )

        assert session.session_id == "sess_abc123"
        assert session.scope.client_id == "clinic_001"
        assert session.current_phase == AssessmentPhase.RECON
        assert session.recon_result is None
        assert session.completed_at is None
        assert len(session.errors) == 0

    @pytest.mark.asyncio
    async def test_start_requires_ollama_available(self, tmp_path: Path) -> None:
        """Start raises RuntimeError if Ollama is unavailable."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.0/24"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        workflow = AssessmentWorkflow(scope=scope)

        # Mock Ollama unavailable
        with patch.object(
            workflow.llm, "is_available", new_callable=AsyncMock
        ) as mock_available:
            mock_available.return_value = False

            with pytest.raises(RuntimeError, match="Ollama not available"):
                await workflow.start()

    @pytest.mark.asyncio
    async def test_start_creates_session(self, tmp_path: Path) -> None:
        """Start creates session in RECON phase when Ollama available."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.0/24"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        workflow = AssessmentWorkflow(scope=scope, db_path=tmp_path / "test.db")

        # Mock Ollama available and session persistence
        with patch.object(
            workflow.llm, "is_available", new_callable=AsyncMock
        ) as mock_available, patch.object(
            workflow, "_persist_session"
        ) as mock_persist:
            mock_available.return_value = True

            session = await workflow.start()

            assert session.session_id is not None
            assert session.current_phase == AssessmentPhase.RECON
            assert session.scope.client_id == "clinic_001"
            mock_persist.assert_called_once()


class TestExploitationGates:
    """HITL gate protection tests."""

    def test_exploit_blocked_without_approval(self, tmp_path: Path) -> None:
        """Run exploitation raises PermissionError if checkpoint not approved."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.0/24"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        workflow = AssessmentWorkflow(scope=scope, db_path=tmp_path / "test.db")

        session = AssessmentSession(
            session_id="sess_test",
            scope=scope,
            current_phase=AssessmentPhase.EXPLOIT,
        )

        # Create a PENDING checkpoint
        checkpoint = HITLCheckpoint(
            checkpoint_id="cp_123",
            phase=AssessmentPhase.EXPLOIT,
            context={},
            status=HITLStatus.PENDING,
            created_at="2026-05-20T12:00:00",
        )

        import asyncio

        with pytest.raises(PermissionError, match="approval required"):
            asyncio.run(workflow.run_exploitation(session, checkpoint))

    def test_exploit_allowed_after_approval(self, tmp_path: Path) -> None:
        """Run exploitation with approved checkpoint requires scan results."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.0/24"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        workflow = AssessmentWorkflow(scope=scope, db_path=tmp_path / "test.db")

        session = AssessmentSession(
            session_id="sess_test",
            scope=scope,
            current_phase=AssessmentPhase.EXPLOIT,
        )

        # Create an APPROVED checkpoint
        checkpoint = HITLCheckpoint(
            checkpoint_id="cp_123",
            phase=AssessmentPhase.EXPLOIT,
            context={},
            status=HITLStatus.APPROVED,
            created_at="2026-05-20T12:00:00",
            resolved_at="2026-05-20T12:05:00",
            resolver="analyst@clinic.com",
        )

        import asyncio

        # Should raise ValueError for missing scan results, not PermissionError
        with pytest.raises(ValueError, match="scan phase"):
            asyncio.run(workflow.run_exploitation(session, checkpoint))

    @pytest.mark.asyncio
    async def test_request_exploit_approval_creates_checkpoint(
        self, tmp_path: Path
    ) -> None:
        """Request exploit approval creates HITL checkpoint."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.0/24"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        workflow = AssessmentWorkflow(scope=scope, db_path=tmp_path / "test.db")

        session = AssessmentSession(
            session_id="sess_test",
            scope=scope,
            current_phase=AssessmentPhase.SCAN,
        )

        # Add scan results
        vuln = VulnFinding(
            host="192.168.1.1",
            port=22,
            service="ssh",
            cve="CVE-2023-1234",
            cvss_score=7.5,
            severity="high",
            description="SSH version vulnerable to auth bypass",
            remediation="Update SSH to latest version",
        )
        session.scan_result = ScanResult(
            findings=[vuln],
            total_critical=0,
            total_high=1,
            risk_summary="One high-severity finding detected",
        )

        # Mock session persistence
        with patch.object(workflow, "_persist_session"):
            checkpoint = await workflow.request_exploit_approval(session, session.scan_result)

        assert checkpoint.status == HITLStatus.PENDING
        assert checkpoint.phase == AssessmentPhase.EXPLOIT
        assert checkpoint.context["high_findings"] == 1
        assert session.hitl_checkpoint == checkpoint

    @pytest.mark.asyncio
    async def test_request_approval_without_scan_raises(
        self, tmp_path: Path
    ) -> None:
        """Request exploit approval without scan results raises ValueError."""
        scope = AssessmentScope(
            client_id="clinic_001",
            target_hosts=["192.168.1.0/24"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT_2026_05_20",
        )
        workflow = AssessmentWorkflow(scope=scope, db_path=tmp_path / "test.db")

        session = AssessmentSession(
            session_id="sess_test",
            scope=scope,
            current_phase=AssessmentPhase.SCAN,
            scan_result=None,  # No scan results
        )

        with pytest.raises(ValueError, match="scan results"):
            await workflow.request_exploit_approval(session, ScanResult([], 0, 0, ""))


class TestVulnFindings:
    """Vulnerability finding dataclass tests."""

    def test_vuln_finding_creation(self) -> None:
        """VulnFinding dataclass creates finding record."""
        finding = VulnFinding(
            host="192.168.1.100",
            port=3306,
            service="mysql",
            cve="CVE-2021-1234",
            cvss_score=8.2,
            severity="high",
            description="MySQL version vulnerable to RCE",
            remediation="Update MySQL to 8.0.25 or later",
        )

        assert finding.host == "192.168.1.100"
        assert finding.port == 3306
        assert finding.service == "mysql"
        assert finding.cvss_score == 8.2
        assert finding.severity == "high"


class TestIntegration:
    """Integration tests combining multiple components."""

    def test_hitl_gate_persistence_across_sessions(self, tmp_path: Path) -> None:
        """HITL checkpoints persist across gate instances."""
        db_path = tmp_path / "test_sessions.db"

        # Create and save checkpoint
        gate1 = HITLGate(db_path=db_path)
        checkpoint1 = gate1.create_checkpoint(
            session_id="sess_123",
            phase=AssessmentPhase.EXPLOIT,
            context={"critical": 2},
        )
        checkpoint_id = checkpoint1.checkpoint_id

        # Retrieve with new gate instance
        gate2 = HITLGate(db_path=db_path)
        checkpoint2 = gate2.get_checkpoint(checkpoint_id)

        assert checkpoint2 is not None
        assert checkpoint2.checkpoint_id == checkpoint_id
        assert checkpoint2.context["critical"] == 2

    def test_workflow_scope_validation_integration(self) -> None:
        """AssessmentWorkflow scope validation with multiple CIDR ranges."""
        scope = AssessmentScope(
            client_id="hospital_001",
            target_hosts=[
                "192.168.1.0/24",
                "10.20.0.0/16",
                "clinic-main.local",
            ],
            target_type=TargetType.HOSPITAL,
            authorized_techniques=["port_scan", "vuln_scan"],
            contract_ref="HOSP_2026_CONTRACT",
        )
        workflow = AssessmentWorkflow(scope=scope)

        # Valid: in first CIDR
        assert workflow._validate_scope("192.168.1.50") is True

        # Valid: in second CIDR
        assert workflow._validate_scope("10.20.100.200") is True

        # Valid: hostname match
        assert workflow._validate_scope("clinic-main.local") is True

        # Invalid: outside both CIDR ranges
        assert workflow._validate_scope("172.16.0.1") is False


class TestRunRecon:
    """Reconnaissance phase integration tests."""

    @pytest.mark.asyncio
    async def test_recon_nmap_not_found(self, tmp_path: Path) -> None:
        """run_recon raises RuntimeError when nmap is not installed."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="test-session",
            scope=scope,
            current_phase=AssessmentPhase.RECON,
        )
        with patch(
            "asyncio.create_subprocess_exec", side_effect=FileNotFoundError
        ):
            with pytest.raises(RuntimeError, match="nmap not found"):
                await workflow.run_recon(session)

    @pytest.mark.asyncio
    async def test_recon_scope_validation_blocks_out_of_scope(
        self, tmp_path: Path
    ) -> None:
        """run_recon raises PermissionError for hosts outside scope."""
        scope = AssessmentScope(
            client_id="test",
            target_hosts=["192.168.1.1"],
            target_type=TargetType.CLINIC,
            authorized_techniques=["port_scan"],
            contract_ref="CONTRACT-001",
        )
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        # Create session with out-of-scope host
        session = AssessmentSession(
            session_id="test-session",
            scope=AssessmentScope(
                client_id="test",
                target_hosts=["10.0.0.1"],  # NOT in scope
                target_type=TargetType.CLINIC,
                authorized_techniques=["port_scan"],
                contract_ref="CONTRACT-001",
            ),
            current_phase=AssessmentPhase.RECON,
        )
        # Override workflow scope to only allow 192.168.1.1
        workflow.scope = scope

        with pytest.raises(PermissionError):
            await workflow.run_recon(session)

    @pytest.mark.asyncio
    async def test_recon_success_parses_nmap_output(
        self, tmp_path: Path
    ) -> None:
        """run_recon successfully parses nmap XML output."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="test-session",
            scope=scope,
            current_phase=AssessmentPhase.RECON,
        )

        # Mock nmap XML output
        nmap_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<nmaprun scanner="nmap" args="nmap -sV -sC -T4 --open -oX - 192.168.1.1" start="1" startstr="Mon May 20 12:00:00 2026" version="7.92" xmloutputversion="1.05">
<host starttime="1" endtime="1">
<status state="up" reason="echo-reply" reason_ttl="0"/>
<address addr="192.168.1.1" addrtype="ipv4"/>
<hostnames>
</hostnames>
<ports>
<port protocol="tcp" portid="22">
<state state="open" reason="syn-ack" reason_ttl="0"/>
<service name="ssh" product="OpenSSH" version="7.2p2" extrainfo="protocol 2.0" ostype="Linux" method="table" conf="10"/>
</port>
<port protocol="tcp" portid="80">
<state state="open" reason="syn-ack" reason_ttl="0"/>
<service name="http" product="Apache httpd" version="2.4.6" extrainfo="(CentOS)" method="table" conf="10"/>
</port>
</ports>
</host>
</nmaprun>"""

        async def mock_exec(*args, **kwargs):
            # Create mock process
            proc = MagicMock()
            proc.communicate = AsyncMock(return_value=(nmap_xml, b""))
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_exec):
            result = await workflow.run_recon(session)

        assert isinstance(result, ReconResult)
        assert len(result.hosts_discovered) == 1
        assert result.hosts_discovered[0]["host"] == "192.168.1.1"
        assert len(result.hosts_discovered[0]["ports"]) == 2
        assert result.hosts_discovered[0]["ports"][0]["port"] == 22
        assert result.hosts_discovered[0]["ports"][0]["service"] == "ssh"

    @pytest.mark.asyncio
    async def test_recon_timeout_raises_runtime_error(
        self, tmp_path: Path
    ) -> None:
        """run_recon raises RuntimeError on asyncio.TimeoutError."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="test-session",
            scope=scope,
            current_phase=AssessmentPhase.RECON,
        )

        async def mock_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        with patch(
            "asyncio.create_subprocess_exec"
        ), patch(
            "asyncio.wait_for", side_effect=asyncio.TimeoutError
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                await workflow.run_recon(session)


class TestRunScan:
    """Vulnerability scan phase tests."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_scan_returns_findings(self, tmp_path: Path) -> None:
        """run_scan returns ScanResult with VulnFindings from LLM analysis.

        Requiere análisis LLM real (Ollama) → integración, deseleccionado en CI.
        """
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="test-session",
            scope=scope,
            current_phase=AssessmentPhase.SCAN,
        )
        recon = ReconResult(
            hosts_discovered=[
                {
                    "host": "192.168.1.1",
                    "ports": [
                        {
                            "port": 22,
                            "service": "ssh",
                            "version": "7.2p2",
                            "product": "OpenSSH",
                        }
                    ],
                }
            ],
            raw_output="<nmap>...</nmap>",
            timestamp=datetime.now(UTC).isoformat(),
        )
        mock_response = json.dumps(
            {
                "cve": "CVE-2016-6515",
                "cvss_score": 7.8,
                "severity": "high",
                "description": "OpenSSH 7.2p2 DoS vulnerability",
                "remediation": "Upgrade to OpenSSH 8.x or later",
            }
        )
        with patch.object(
            workflow.llm, "query_fast", return_value=mock_response
        ):
            result = await workflow.run_scan(session, recon)
        assert isinstance(result, ScanResult)
        assert len(result.findings) >= 1
        assert result.findings[0].host == "192.168.1.1"
        assert result.findings[0].port == 22

    @pytest.mark.asyncio
    async def test_scan_handles_invalid_llm_json(self, tmp_path: Path) -> None:
        """run_scan gracefully handles non-JSON LLM responses."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="test-session",
            scope=scope,
            current_phase=AssessmentPhase.SCAN,
        )
        recon = ReconResult(
            hosts_discovered=[
                {
                    "host": "192.168.1.1",
                    "ports": [
                        {
                            "port": 65432,
                            "service": "unknown_service",
                            "version": "",
                            "product": "UnknownApp",
                        }
                    ],
                }
            ],
            raw_output="",
            timestamp=datetime.now(UTC).isoformat(),
        )
        with patch.object(
            workflow.llm, "query_fast", return_value="not valid json"
        ):
            result = await workflow.run_scan(session, recon)
        assert isinstance(result, ScanResult)
        # Should have fallback finding with info severity when LLM fails
        assert result.findings[0].severity == "info"


class TestGenerateReport:
    """Report generation tests."""

    @pytest.mark.asyncio
    async def test_generate_report_creates_file(self, tmp_path: Path) -> None:
        """generate_report saves Markdown file and returns path."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="test-session",
            scope=scope,
            current_phase=AssessmentPhase.REPORT,
        )
        session.recon_result = ReconResult(
            hosts_discovered=[],
            raw_output="",
            timestamp=datetime.now(UTC).isoformat(),
        )
        session.scan_result = ScanResult(
            findings=[
                VulnFinding(
                    host="192.168.1.1",
                    port=22,
                    service="ssh",
                    cve="CVE-2016-6515",
                    cvss_score=7.8,
                    severity="high",
                    description="Test vuln",
                    remediation="Upgrade",
                )
            ],
            total_critical=0,
            total_high=1,
            risk_summary="High risk environment.",
            timestamp=datetime.now(UTC).isoformat(),
        )
        with patch.object(
            workflow.llm,
            "query_quality",
            return_value="1. Update all systems. / Actualizar todos los sistemas.",
        ):
            report_path = await workflow.generate_report(session)
        assert report_path.endswith(".md")
        assert Path(report_path).exists()
        content = Path(report_path).read_text()
        assert "Security Assessment Report" in content

    @pytest.mark.asyncio
    async def test_generate_report_requires_recon_and_scan(
        self, tmp_path: Path
    ) -> None:
        """generate_report raises ValueError without recon/scan results."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="test-session",
            scope=scope,
            current_phase=AssessmentPhase.REPORT,
        )
        # No recon_result or scan_result

        with pytest.raises(ValueError, match="requires completed"):
            await workflow.generate_report(session)


class TestPersistSession:
    """Session persistence tests."""

    def test_persist_session_creates_table_and_record(
        self, tmp_path: Path
    ) -> None:
        """_persist_session creates table and inserts session record."""
        scope = make_scope()
        db_path = tmp_path / "test.db"
        workflow = AssessmentWorkflow(scope, db_path=db_path)
        session = AssessmentSession(
            session_id="persist-test",
            scope=scope,
            current_phase=AssessmentPhase.RECON,
        )
        workflow._persist_session(session)
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT session_id, current_phase FROM assessment_sessions WHERE session_id=?",
            ("persist-test",),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "persist-test"
        assert row[1] == "recon"

    def test_persist_session_stores_scan_results(
        self, tmp_path: Path
    ) -> None:
        """_persist_session serializes scan results correctly."""
        scope = make_scope()
        db_path = tmp_path / "test.db"
        workflow = AssessmentWorkflow(scope, db_path=db_path)

        finding = VulnFinding(
            host="192.168.1.1",
            port=22,
            service="ssh",
            cve="CVE-2016-6515",
            cvss_score=7.8,
            severity="high",
            description="Test vulnerability",
            remediation="Upgrade SSH",
        )
        scan_result = ScanResult(
            findings=[finding],
            total_critical=0,
            total_high=1,
            risk_summary="One high finding",
            timestamp=datetime.now(UTC).isoformat(),
        )

        session = AssessmentSession(
            session_id="persist-scan-test",
            scope=scope,
            current_phase=AssessmentPhase.SCAN,
            scan_result=scan_result,
        )
        workflow._persist_session(session)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT scan_json FROM assessment_sessions WHERE session_id=?",
            ("persist-scan-test",),
        ).fetchone()
        conn.close()

        assert row is not None
        scan_json = json.loads(row[0])
        assert scan_json["total_high"] == 1
        assert len(scan_json["findings"]) == 1


class TestGeneratePdfReport:
    """PDF report generation tests."""

    @pytest.mark.asyncio
    async def test_pdf_requires_report_path(self, tmp_path: Path) -> None:
        """generate_pdf_report raises ValueError if no report_path in session."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="test", scope=scope, current_phase=AssessmentPhase.REPORT
        )
        # report_path is None
        with pytest.raises(ValueError, match="generate_report"):
            await workflow.generate_pdf_report(session)

    @pytest.mark.asyncio
    async def test_pdf_missing_md_file_raises(self, tmp_path: Path) -> None:
        """generate_pdf_report raises ValueError if .md file doesn't exist."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="test", scope=scope, current_phase=AssessmentPhase.REPORT
        )
        session.report_path = str(tmp_path / "nonexistent.md")
        with pytest.raises(ValueError, match="not found"):
            await workflow.generate_pdf_report(session)

    @pytest.mark.asyncio
    async def test_pdf_pandoc_not_found_raises(self, tmp_path: Path) -> None:
        """generate_pdf_report raises RuntimeError when pandoc missing."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        md_file = tmp_path / "report.md"
        md_file.write_text("# Test Report\n\nContent here.")
        session = AssessmentSession(
            session_id="test", scope=scope, current_phase=AssessmentPhase.REPORT
        )
        session.report_path = str(md_file)
        with patch(
            "asyncio.create_subprocess_exec", side_effect=FileNotFoundError
        ):
            with pytest.raises(RuntimeError, match="pandoc not found"):
                await workflow.generate_pdf_report(session)

    @pytest.mark.asyncio
    async def test_pdf_success_returns_path(self, tmp_path: Path) -> None:
        """generate_pdf_report returns PDF path when pandoc succeeds."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        md_file = tmp_path / "report.md"
        md_file.write_text("# Test Report\n\nSecurity findings here.")
        pdf_file = tmp_path / "report.pdf"
        pdf_file.write_bytes(b"%PDF-1.4 fake")  # simulate pandoc output
        session = AssessmentSession(
            session_id="test", scope=scope, current_phase=AssessmentPhase.REPORT
        )
        session.report_path = str(md_file)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await workflow.generate_pdf_report(session)
        assert result.endswith(".pdf")
        assert result == str(pdf_file)

    @pytest.mark.asyncio
    async def test_pdf_timeout_raises_runtime_error(self, tmp_path: Path) -> None:
        """generate_pdf_report raises RuntimeError on timeout."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        md_file = tmp_path / "report.md"
        md_file.write_text("# Test Report\n\nContent.")
        session = AssessmentSession(
            session_id="test", scope=scope, current_phase=AssessmentPhase.REPORT
        )
        session.report_path = str(md_file)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(RuntimeError, match="timed out"):
                await workflow.generate_pdf_report(session)

    @pytest.mark.asyncio
    async def test_pdf_conversion_failure_tries_fallbacks(
        self, tmp_path: Path
    ) -> None:
        """generate_pdf_report tries fallback engines on failure."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        md_file = tmp_path / "report.md"
        md_file.write_text("# Test Report\n\nContent.")
        session = AssessmentSession(
            session_id="test", scope=scope, current_phase=AssessmentPhase.REPORT
        )
        session.report_path = str(md_file)

        # First two attempts fail, third succeeds
        call_count = 0

        async def mock_exec_with_fallback(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            proc = AsyncMock()
            if call_count < 3:
                # First two calls fail
                proc.returncode = 1
                proc.communicate = AsyncMock(
                    return_value=(b"", b"Engine not available")
                )
            else:
                # Third call (latex fallback) succeeds
                pdf_path = tmp_path / "report.pdf"
                pdf_path.write_bytes(b"%PDF-1.4")
                proc.returncode = 0
                proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        with patch(
            "asyncio.create_subprocess_exec", side_effect=mock_exec_with_fallback
        ):
            result = await workflow.generate_pdf_report(session)
        assert result.endswith(".pdf")
        assert call_count == 3  # Tried 3 engines


class TestRunExploitation:
    """Exploitation phase tests with HITL gate and audit trail."""

    @pytest.mark.asyncio
    async def test_exploitation_requires_approved_checkpoint(
        self, tmp_path: Path
    ) -> None:
        """run_exploitation raises PermissionError for non-approved checkpoint."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="exploit-test",
            scope=scope,
            current_phase=AssessmentPhase.EXPLOIT,
        )
        session.scan_result = ScanResult(
            findings=[],
            total_critical=0,
            total_high=0,
            risk_summary="",
        )

        checkpoint = HITLCheckpoint(
            checkpoint_id="cp-pending",
            phase=AssessmentPhase.EXPLOIT,
            context={},
            status=HITLStatus.PENDING,
            created_at=datetime.now(UTC).isoformat(),
        )

        with pytest.raises(PermissionError, match="approval required"):
            await workflow.run_exploitation(session, checkpoint)

    @pytest.mark.asyncio
    async def test_exploitation_requires_scan_results(
        self, tmp_path: Path
    ) -> None:
        """run_exploitation raises ValueError if session has no scan results."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="exploit-test",
            scope=scope,
            current_phase=AssessmentPhase.EXPLOIT,
            scan_result=None,
        )

        checkpoint = HITLCheckpoint(
            checkpoint_id="cp-approved",
            phase=AssessmentPhase.EXPLOIT,
            context={},
            status=HITLStatus.APPROVED,
            created_at=datetime.now(UTC).isoformat(),
            resolved_at=datetime.now(UTC).isoformat(),
            resolver="admin@example.com",
        )

        with pytest.raises(ValueError, match="scan phase"):
            await workflow.run_exploitation(session, checkpoint)

    @pytest.mark.asyncio
    async def test_exploitation_returns_structured_dict(
        self, tmp_path: Path
    ) -> None:
        """run_exploitation returns dict with all required keys."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="exploit-test",
            scope=scope,
            current_phase=AssessmentPhase.EXPLOIT,
        )
        session.scan_result = ScanResult(
            findings=[
                VulnFinding(
                    host="192.168.1.1",
                    port=22,
                    service="ssh",
                    cve="CVE-2016-6515",
                    cvss_score=7.8,
                    severity="high",
                    description="OpenSSH 7.2p2 DoS",
                    remediation="Upgrade",
                )
            ],
            total_critical=0,
            total_high=1,
            risk_summary="High risk.",
            timestamp=datetime.now(UTC).isoformat(),
        )

        checkpoint = HITLCheckpoint(
            checkpoint_id="cp-test",
            phase=AssessmentPhase.EXPLOIT,
            context={"scan_findings": 1},
            status=HITLStatus.APPROVED,
            created_at=datetime.now(UTC).isoformat(),
            resolved_at=datetime.now(UTC).isoformat(),
            resolver="admin@example.com",
        )

        mock_llm_response = json.dumps(
            {
                "technique": "SSH version DoS via crafted packets",
                "prerequisites": ["network access to port 22"],
                "impact": "Service disruption",
                "difficulty": "medium",
                "mitre_ttp": "T1499",
            }
        )

        # V17: la técnica de exploit la produce el cerebro Claude+CVP inyectado (cve_analyst),
        # no el local. Inyectamos un cerebro canned que devuelve el JSON de técnica.
        async def _brain(directive: dict) -> str:
            return mock_llm_response

        workflow.cve_analyst = _brain
        with patch.object(workflow, "_persist_session"):
            result = await workflow.run_exploitation(session, checkpoint)

        assert "session_id" in result
        assert "engagement_id" in result
        assert "checkpoint_id" in result
        assert "approved_by" in result
        assert "findings_analyzed" in result
        assert "techniques_documented" in result
        assert "audit_logged" in result
        assert "note" in result
        assert result["findings_analyzed"] == 1
        assert result["approved_by"] == "admin@example.com"
        assert result["audit_logged"] is True
        assert len(result["techniques_documented"]) == 1
        assert (
            result["techniques_documented"][0]["technique"]
            == "SSH version DoS via crafted packets"
        )

    @pytest.mark.asyncio
    async def test_exploitation_only_processes_high_severity(
        self, tmp_path: Path
    ) -> None:
        """run_exploitation only documents findings with CVSS >= 7.0."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="exploit-filter-test",
            scope=scope,
            current_phase=AssessmentPhase.EXPLOIT,
        )
        session.scan_result = ScanResult(
            findings=[
                VulnFinding(
                    host="192.168.1.1",
                    port=22,
                    service="ssh",
                    cve="CVE-HIGH",
                    cvss_score=8.0,
                    severity="high",
                    description="High finding",
                    remediation="Fix",
                ),
                VulnFinding(
                    host="192.168.1.1",
                    port=80,
                    service="http",
                    cve=None,
                    cvss_score=3.0,
                    severity="low",
                    description="Low finding",
                    remediation="Fix",
                ),
            ],
            total_critical=0,
            total_high=1,
            risk_summary="Mixed.",
            timestamp=datetime.now(UTC).isoformat(),
        )

        checkpoint = HITLCheckpoint(
            checkpoint_id="cp-filter",
            phase=AssessmentPhase.EXPLOIT,
            context={},
            status=HITLStatus.APPROVED,
            created_at=datetime.now(UTC).isoformat(),
            resolved_at=datetime.now(UTC).isoformat(),
            resolver="test",
        )

        with patch.object(
            workflow.llm,
            "query_fast",
            return_value=json.dumps(
                {
                    "technique": "test",
                    "prerequisites": [],
                    "impact": "test",
                    "difficulty": "easy",
                    "mitre_ttp": "T1234",
                }
            ),
        ), patch.object(workflow, "_persist_session"):
            result = await workflow.run_exploitation(session, checkpoint)

        # Only 1 finding (CVSS 8.0) should be documented, not the low one (CVSS 3.0)
        assert result["findings_analyzed"] == 1
        assert len(result["techniques_documented"]) == 1
        assert result["techniques_documented"][0]["cvss_score"] == 8.0

    @pytest.mark.asyncio
    async def test_exploitation_audit_persisted_to_db(
        self, tmp_path: Path
    ) -> None:
        """run_exploitation writes audit records to exploitation_audit table."""
        scope = make_scope()
        db_path = tmp_path / "test.db"
        workflow = AssessmentWorkflow(scope, db_path=db_path)
        session = AssessmentSession(
            session_id="audit-test",
            scope=scope,
            current_phase=AssessmentPhase.EXPLOIT,
        )
        session.scan_result = ScanResult(
            findings=[
                VulnFinding(
                    host="10.0.0.1",
                    port=3389,
                    service="rdp",
                    cve="CVE-2019-0708",
                    cvss_score=9.8,
                    severity="critical",
                    description="BlueKeep RDP",
                    remediation="Patch",
                )
            ],
            total_critical=1,
            total_high=0,
            risk_summary="Critical.",
            timestamp=datetime.now(UTC).isoformat(),
        )

        checkpoint = HITLCheckpoint(
            checkpoint_id="cp-audit",
            phase=AssessmentPhase.EXPLOIT,
            context={},
            status=HITLStatus.APPROVED,
            created_at=datetime.now(UTC).isoformat(),
            resolved_at=datetime.now(UTC).isoformat(),
            resolver="auditor",
        )

        # V17: cerebro Claude+CVP inyectado (no el local).
        async def _brain(directive: dict) -> str:
            return json.dumps(
                {
                    "technique": "BlueKeep exploitation",
                    "prerequisites": ["unpatched RDP"],
                    "impact": "RCE",
                    "difficulty": "hard",
                    "mitre_ttp": "T1210",
                }
            )

        workflow.cve_analyst = _brain
        with patch.object(workflow, "_persist_session"):
            await workflow.run_exploitation(session, checkpoint)

        # Verify audit table has the record
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT * FROM exploitation_audit WHERE session_id=?",
            ("audit-test",),
        ).fetchall()
        conn.close()

        assert len(rows) >= 1
        # Check fields: session_id, engagement_id, timestamp, finding_host, finding_port,
        # finding_cve, technique_used, checkpoint_id, approved_by, outcome
        assert rows[0][1] == "audit-test"  # session_id
        assert rows[0][4] == "10.0.0.1"  # finding_host
        assert rows[0][5] == 3389  # finding_port
        assert rows[0][6] == "CVE-2019-0708"  # finding_cve
        assert "BlueKeep" in rows[0][7]  # technique_used
        assert rows[0][9] == "auditor"  # approved_by

    @pytest.mark.asyncio
    async def test_exploitation_handles_invalid_llm_json(
        self, tmp_path: Path
    ) -> None:
        """run_exploitation gracefully handles non-JSON LLM responses."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="exploit-invalid-json",
            scope=scope,
            current_phase=AssessmentPhase.EXPLOIT,
        )
        session.scan_result = ScanResult(
            findings=[
                VulnFinding(
                    host="192.168.1.1",
                    port=443,
                    service="https",
                    cve="CVE-2021-5678",
                    cvss_score=7.5,
                    severity="high",
                    description="TLS vulnerability",
                    remediation="Upgrade TLS",
                )
            ],
            total_critical=0,
            total_high=1,
            risk_summary="TLS issue.",
            timestamp=datetime.now(UTC).isoformat(),
        )

        checkpoint = HITLCheckpoint(
            checkpoint_id="cp-invalid",
            phase=AssessmentPhase.EXPLOIT,
            context={},
            status=HITLStatus.APPROVED,
            created_at=datetime.now(UTC).isoformat(),
            resolved_at=datetime.now(UTC).isoformat(),
            resolver="reviewer",
        )

        with patch.object(
            workflow.llm, "query_fast", return_value="not valid json"
        ), patch.object(workflow, "_persist_session"):
            result = await workflow.run_exploitation(session, checkpoint)

        # Should still return valid result with fallback data
        assert result["findings_analyzed"] == 1
        assert len(result["techniques_documented"]) == 1
        # Fallback should have "Manual technique assessment required"
        assert "required" in result["techniques_documented"][0]["technique"].lower()

    @pytest.mark.asyncio
    async def test_exploitation_updates_session_phase(
        self, tmp_path: Path
    ) -> None:
        """run_exploitation updates session phase to REPORT."""
        scope = make_scope()
        workflow = AssessmentWorkflow(scope, db_path=tmp_path / "test.db")
        session = AssessmentSession(
            session_id="phase-test",
            scope=scope,
            current_phase=AssessmentPhase.EXPLOIT,
        )
        session.scan_result = ScanResult(
            findings=[
                VulnFinding(
                    host="192.168.1.1",
                    port=22,
                    service="ssh",
                    cve="CVE-2016-6515",
                    cvss_score=7.8,
                    severity="high",
                    description="SSH DoS",
                    remediation="Upgrade",
                )
            ],
            total_critical=0,
            total_high=1,
            risk_summary="High risk.",
            timestamp=datetime.now(UTC).isoformat(),
        )

        checkpoint = HITLCheckpoint(
            checkpoint_id="cp-phase",
            phase=AssessmentPhase.EXPLOIT,
            context={},
            status=HITLStatus.APPROVED,
            created_at=datetime.now(UTC).isoformat(),
            resolved_at=datetime.now(UTC).isoformat(),
            resolver="reviewer",
        )

        with patch.object(
            workflow.llm,
            "query_fast",
            return_value=json.dumps(
                {
                    "technique": "DoS attack",
                    "prerequisites": ["network access"],
                    "impact": "service down",
                    "difficulty": "easy",
                    "mitre_ttp": "T1499",
                }
            ),
        ):
            await workflow.run_exploitation(session, checkpoint)

        # Verify session phase updated
        assert session.current_phase == AssessmentPhase.REPORT
