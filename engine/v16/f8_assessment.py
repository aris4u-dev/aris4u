"""F8.ASSESSMENT — Healthcare Pentest Workflow Orchestrator.

⚠️ NO CABLEADO EN RUNTIME (verificado 2026-06-16). Este módulo (1434 LOC) + f8_vuln_kb.py
pertenecen al VERTICAL PENTEST diferido (Tramo 4): NO tienen callers en hooks/dispatch/,
v16_orchestrator ni integrations/ — solo los importan sus propios tests. NO se ejecutan en
el flujo normal de ARIS4U. No lo actives asumiendo que ya corre; reactivarlo es una
decisión de producto sobre el vertical, no un detalle de implementación.

Four-phase pipeline: Recon → Scan → Exploit (HITL) → Report.
Cerebro (V17 Tramo 1, Fable 2026-07-02): la cognición de CVE/técnica (infra metadata, NO PHI)
la ejecuta Claude+CVP vía `cve_analyst` inyectado por el orquestador ("cognición rentada");
Ollama local queda SOLO para payloads con PHI (never-egress). El discriminador es el PAYLOAD,
no el vertical (ver `_payload_has_phi`). HITL gate requiere aprobación humana antes de exploit.

Architecture:
    1. AssessmentSession — state container for a single engagement
    2. HITLGate — human-in-the-loop checkpoint (blocks until approved)
    3. LLMRouter — selects fast vs quality model based on task type
    4. AssessmentWorkflow — orchestrates all four phases

Attributes:
    AssessmentPhase: Enum of workflow phases (recon, scan, exploit, report)
    HITLStatus: Checkpoint approval status (pending, approved, rejected)
    TargetType: Target infrastructure type (clinic, hospital, ems, health_system)
    LLMRouter: Routes tasks to local Ollama models (never external APIs)
    HITLGate: Human-in-the-loop approval checkpoint manager
    AssessmentWorkflow: Master orchestrator for healthcare pentest engagements
"""

from __future__ import annotations

import asyncio
import httpx
import json
import re
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from ipaddress import IPv4Address, IPv4Network, AddressValueError
from pathlib import Path
from typing import Any, Optional
from collections.abc import Awaitable, Callable

from .config import OLLAMA_MAC_URL, MAC_MODELS, SESSIONS_DB
from .f8_vuln_kb import HealthcareVulnKB

# V17 Tramo 1 (cerebro→Claude+CVP, veredicto Fable 2026-07-02): el discriminador local-vs-
# Claude NO es "el vertical es healthcare" (f8 SIEMPRE corre healthcare → enrutaría TODO a
# local = el fix se autodestruye en silencio, tests verdes). Es "ESTE payload contiene PHI".
# recon/scan/CVE/técnica = metadata de infraestructura, NUNCA PHI → cognición a Claude+CVP.
# Solo un artefacto con datos de paciente iría a local (PHI-safe, never-egress).
_PHI_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                      # SSN
    re.compile(r"\bMRN[:#]?\s*\d+\b", re.I),                   # medical record number
    re.compile(r"\b(patient|paciente|diagnos|dob|date of birth|hl7|icd-?10)\b", re.I),
]


def _payload_has_phi(*parts: object) -> bool:
    """True si algún fragmento del payload contiene señales de PHI (datos de paciente).

    Payload-based, NO vertical-based (corrección Fable): scan/CVE = infra, no PHI.
    """
    blob = " ".join(str(p) for p in parts if p)
    return any(p.search(blob) for p in _PHI_PATTERNS)


class AssessmentPhase(Enum):
    """Workflow phase enumeration."""

    RECON = "recon"
    SCAN = "scan"
    EXPLOIT = "exploit"
    REPORT = "report"


class HITLStatus(Enum):
    """Human-in-the-loop checkpoint status."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class TargetType(Enum):
    """Target infrastructure type classification."""

    CLINIC = "clinic"
    HOSPITAL = "hospital"
    EMS = "ems"
    HEALTH_SYSTEM = "health_system"


@dataclass
class AssessmentScope:
    """Authorization scope from signed engagement contract.

    Attributes:
        client_id: Unique client identifier
        target_hosts: List of authorized IP ranges / hostnames
        target_type: Target infrastructure type
        authorized_techniques: Engagement-approved attack techniques
        contract_ref: Reference to signed authorization document
        engagement_id: Unique engagement identifier (generated)
    """

    client_id: str
    target_hosts: list[str]
    target_type: TargetType
    authorized_techniques: list[str]
    contract_ref: str
    engagement_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass
class ReconResult:
    """Reconnaissance phase output.

    Attributes:
        hosts_discovered: List of discovered hosts with port/service data
        raw_output: Unprocessed reconnaissance tool output
        timestamp: ISO timestamp of execution
    """

    hosts_discovered: list[dict[str, Any]]
    raw_output: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class VulnFinding:
    """Single vulnerability finding.

    Attributes:
        host: Target host IP or hostname
        port: Service port number
        service: Service name/identifier
        cve: CVE identifier (optional)
        cvss_score: CVSS v3 score (0.0-10.0)
        severity: Severity classification
        description: Vulnerability narrative
        remediation: Recommended fix
    """

    host: str
    port: int
    service: str
    cve: Optional[str]
    cvss_score: float
    severity: str
    description: str
    remediation: str


@dataclass
class ScanResult:
    """Vulnerability scan phase output.

    Attributes:
        findings: List of discovered vulnerabilities
        total_critical: Count of critical severity findings
        total_high: Count of high severity findings
        risk_summary: High-level risk narrative
        timestamp: ISO timestamp of execution
    """

    findings: list[VulnFinding]
    total_critical: int
    total_high: int
    risk_summary: str
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class HITLCheckpoint:
    """Human-in-the-loop approval checkpoint.

    Attributes:
        checkpoint_id: Unique checkpoint identifier
        phase: Workflow phase requiring approval
        context: Execution context for human review
        status: Approval status
        created_at: ISO timestamp of checkpoint creation
        resolved_at: ISO timestamp of approval/rejection
        resolver: User identifier who approved/rejected
    """

    checkpoint_id: str
    phase: AssessmentPhase
    context: dict[str, Any]
    status: HITLStatus
    created_at: str
    resolved_at: Optional[str] = None
    resolver: Optional[str] = None


@dataclass
class AssessmentSession:
    """Session container for a single pentest engagement.

    Attributes:
        session_id: Unique session identifier
        scope: Engagement authorization scope
        current_phase: Current workflow phase
        recon_result: Phase 1 output (optional)
        scan_result: Phase 2 output (optional)
        hitl_checkpoint: HITL gate checkpoint (optional)
        report_path: Path to generated report (optional)
        started_at: ISO timestamp of session start
        completed_at: ISO timestamp of session completion (optional)
        errors: List of error messages encountered
    """

    session_id: str
    scope: AssessmentScope
    current_phase: AssessmentPhase
    recon_result: Optional[ReconResult] = None
    scan_result: Optional[ScanResult] = None
    hitl_checkpoint: Optional[HITLCheckpoint] = None
    report_path: Optional[str] = None
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: Optional[str] = None
    errors: list[str] = field(default_factory=list)


class LLMRouter:
    """Routes tasks to appropriate local Ollama model based on task type.

    Fast tasks (analysis, triage) → qwen35-analyst.
    Quality tasks (final report, complex reasoning) → hermes3:70b.
    PHI-containing data: local only, never routed to external APIs.

    Attributes:
        ollama_url: Base URL for Ollama API
        models: Dictionary of available models from config
    """

    def __init__(self, ollama_url: str = OLLAMA_MAC_URL) -> None:
        """Initialize LLM router.

        Args:
            ollama_url: Ollama API base URL (default localhost:11434)
        """
        self.ollama_url = ollama_url
        self.models = MAC_MODELS

    async def query_fast(
        self, prompt: str, system: Optional[str] = None
    ) -> str:
        """Query fast model (qwen35-analyst) for rapid analysis tasks.

        Args:
            prompt: User prompt/input
            system: Optional system prompt

        Returns:
            Model response string

        Raises:
            httpx.RequestError: If Ollama API is unavailable
            TimeoutError: If request exceeds 30 second timeout
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            payload = {
                "model": self.models["analyst"],
                "prompt": prompt,
                "stream": False,
            }
            if system:
                payload["system"] = system

            response = await client.post(
                f"{self.ollama_url}/api/generate",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("response", "")

    async def query_quality(
        self, prompt: str, system: Optional[str] = None
    ) -> str:
        """Query quality model (hermes3:70b) for final report generation.

        Args:
            prompt: User prompt/input
            system: Optional system prompt

        Returns:
            Model response string

        Raises:
            httpx.RequestError: If Ollama API is unavailable
            TimeoutError: If request exceeds 120 second timeout
        """
        async with httpx.AsyncClient(timeout=120.0) as client:
            payload = {
                "model": "hermes3:70b",
                "prompt": prompt,
                "stream": False,
            }
            if system:
                payload["system"] = system

            response = await client.post(
                f"{self.ollama_url}/api/generate",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("response", "")

    async def is_available(self) -> bool:
        """Check if Ollama is reachable.

        Returns:
            True if Ollama API responds, False otherwise
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.ollama_url}/api/tags")
                return response.status_code == 200
        except (httpx.RequestError, httpx.TimeoutException):
            return False


class HITLGate:
    """Human-in-the-loop approval checkpoint manager.

    Blocks progression to exploitation phase until human explicitly approves.
    Stores checkpoint state in sessions.db for persistence across restarts.

    Attributes:
        db_path: Path to SQLite sessions database
    """

    def __init__(self, db_path: Path = SESSIONS_DB) -> None:
        """Initialize HITL gate.

        Args:
            db_path: Path to SQLite database (default data/sessions.db)
        """
        self.db_path = Path(db_path) if not isinstance(db_path, Path) else db_path
        self._init_checkpoint_table()

    def _init_checkpoint_table(self) -> None:
        """Create hitl_checkpoints table if not exists."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hitl_checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    context TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolver TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_hitl_session
                ON hitl_checkpoints (session_id)
            """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS exploitation_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    engagement_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    finding_host TEXT,
                    finding_port INTEGER,
                    finding_cve TEXT,
                    technique_used TEXT,
                    checkpoint_id TEXT NOT NULL,
                    approved_by TEXT,
                    outcome TEXT,
                    FOREIGN KEY (checkpoint_id) REFERENCES hitl_checkpoints(checkpoint_id)
                )
            """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_exploitation_session
                ON exploitation_audit (session_id)
            """
            )
            conn.commit()

    def create_checkpoint(
        self, session_id: str, phase: AssessmentPhase, context: dict[str, Any]
    ) -> HITLCheckpoint:
        """Create a new HITL checkpoint requiring approval.

        Persists to sessions.db. Returns checkpoint with PENDING status.

        Args:
            session_id: Parent session identifier
            phase: Phase requiring approval
            context: Execution context for human review

        Returns:
            HITLCheckpoint with status=PENDING
        """
        checkpoint_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()

        checkpoint = HITLCheckpoint(
            checkpoint_id=checkpoint_id,
            phase=phase,
            context=context,
            status=HITLStatus.PENDING,
            created_at=now,
        )

        # Persist to database
        import json

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO hitl_checkpoints
                (checkpoint_id, session_id, phase, context, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    checkpoint_id,
                    session_id,
                    phase.value,
                    json.dumps(context),
                    HITLStatus.PENDING.value,
                    now,
                ),
            )
            conn.commit()

        return checkpoint

    def approve(self, checkpoint_id: str, resolver: str) -> HITLCheckpoint:
        """Approve a pending checkpoint. Updates sessions.db.

        Args:
            checkpoint_id: Checkpoint to approve
            resolver: User identifier approving

        Returns:
            Updated HITLCheckpoint with status=APPROVED

        Raises:
            ValueError: If checkpoint not found
        """
        checkpoint = self.get_checkpoint(checkpoint_id)
        if checkpoint is None:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")

        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE hitl_checkpoints
                SET status = ?, resolved_at = ?, resolver = ?
                WHERE checkpoint_id = ?
            """,
                (HITLStatus.APPROVED.value, now, resolver, checkpoint_id),
            )
            conn.commit()

        checkpoint.status = HITLStatus.APPROVED
        checkpoint.resolved_at = now
        checkpoint.resolver = resolver
        return checkpoint

    def reject(
        self, checkpoint_id: str, resolver: str, reason: str = ""
    ) -> HITLCheckpoint:
        """Reject a pending checkpoint. Updates sessions.db.

        Args:
            checkpoint_id: Checkpoint to reject
            resolver: User identifier rejecting
            reason: Optional rejection reason (unused but tracked)

        Returns:
            Updated HITLCheckpoint with status=REJECTED

        Raises:
            ValueError: If checkpoint not found
        """
        checkpoint = self.get_checkpoint(checkpoint_id)
        if checkpoint is None:
            raise ValueError(f"Checkpoint {checkpoint_id} not found")

        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE hitl_checkpoints
                SET status = ?, resolved_at = ?, resolver = ?
                WHERE checkpoint_id = ?
            """,
                (HITLStatus.REJECTED.value, now, resolver, checkpoint_id),
            )
            conn.commit()

        checkpoint.status = HITLStatus.REJECTED
        checkpoint.resolved_at = now
        checkpoint.resolver = resolver
        return checkpoint

    def get_checkpoint(self, checkpoint_id: str) -> Optional[HITLCheckpoint]:
        """Retrieve checkpoint by ID from sessions.db.

        Args:
            checkpoint_id: Checkpoint identifier to retrieve

        Returns:
            HITLCheckpoint if found, None otherwise
        """
        import json

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT checkpoint_id, phase, context, status, created_at,
                       resolved_at, resolver
                FROM hitl_checkpoints
                WHERE checkpoint_id = ?
            """,
                (checkpoint_id,),
            )
            row = cursor.fetchone()

        if row is None:
            return None

        return HITLCheckpoint(
            checkpoint_id=row[0],
            phase=AssessmentPhase(row[1]),
            context=json.loads(row[2]),
            status=HITLStatus(row[3]),
            created_at=row[4],
            resolved_at=row[5],
            resolver=row[6],
        )


class AssessmentWorkflow:
    """F8 Assessment Orchestrator — master workflow for healthcare pentest engagements.

    Four-phase pipeline with HITL gate between scan and exploitation:
        1. Recon: Network discovery within authorized scope
        2. Scan: Vulnerability identification and CVSS scoring
        3. HITL: Human approval checkpoint before exploitation
        4. Exploit: Controlled demonstration of vulnerabilities
        5. Report: Bilingual assessment narrative generation

    Usage:
        workflow = AssessmentWorkflow(scope)
        session = await workflow.start()
        recon = await workflow.run_recon(session)
        scan = await workflow.run_scan(session, recon)
        # HITL: checkpoint created, human must approve before exploit
        checkpoint = await workflow.request_exploit_approval(session, scan)
        # After human approval:
        # exploit = await workflow.run_exploitation(session, checkpoint)
        report = await workflow.generate_report(session)

    Attributes:
        scope: Engagement authorization scope
        llm: LLM router for model selection
        hitl: HITL gate manager
        session: Current assessment session (optional)
    """

    def __init__(
        self,
        scope: AssessmentScope,
        ollama_url: str = OLLAMA_MAC_URL,
        db_path: Path = SESSIONS_DB,
        cve_analyst: Optional[Callable[[dict], Awaitable[Any]]] = None,
    ) -> None:
        """Initialize assessment workflow.

        Args:
            scope: Engagement authorization scope
            ollama_url: Ollama API URL (default localhost:11434)
            db_path: SQLite database path for persistence
            cve_analyst: V17 — el CEREBRO de análisis de CVE, INYECTADO por el orquestador
                (Claude+CVP). Recibe una directiva (dict con service_info + headers CVP +
                "no payloads") y devuelve el análisis {cve, cvss_score, severity, ...}.
                None = f8 NO fabrica desde el local que alucina: cae a KB/manual (o a Ollama
                local SOLO si el payload tiene PHI). "Cognición rentada del harness": f8 no
                TIENE cerebro, lo ACEPTA. Ver veredicto Fable 2026-07-02.
        """
        self.scope = scope
        self.llm = LLMRouter(ollama_url=ollama_url)
        self.hitl = HITLGate(db_path=db_path)
        self.session: Optional[AssessmentSession] = None
        self.cve_analyst = cve_analyst

    async def _analyze_service(
        self, host_ip: str, port: int, service: str, version: str, product: str
    ) -> dict:
        """V17: análisis servicio→CVE con el cerebro CORRECTO (veredicto Fable 2026-07-02).

        Enrutamiento (payload-based, no vertical-based):
          - cve_analyst inyectado + payload SIN PHI → Claude+CVP (el orquestador). Caso normal.
          - payload CON PHI → Ollama local (PHI-safe, never-egress).
          - sin cve_analyst y sin PHI → NO fabrica desde el local (que ALUCINA CVEs): cae a
            manual. Mejor "revisión manual" honesta que un CVE inventado con tests en verde.
        Solo ANÁLISIS, nunca payloads (la fase exploit refuerza esto en su system_prompt).

        Returns:
            dict {cve, cvss_score, severity, description, remediation}.
        """
        manual = {
            "cve": None, "cvss_score": 0.0, "severity": "info",
            "description": "Manual review required (sin cerebro Claude+CVP para este servicio)",
            "remediation": "Provee cve_analyst (orquestador Claude+CVP) o revisa manualmente.",
        }
        directive = {
            "task": "cve_analysis",
            "cvp": {
                "org": "5a188a34",
                "authorization": "CVP-approved dual-use; assessment scoped by contract_ref",
                "contract_ref": getattr(self.scope, "contract_ref", ""),
            },
            "constraint": ("Analiza vulnerabilidades de infraestructura del servicio. Devuelve "
                           "SOLO JSON {cve, cvss_score, severity, description, remediation}. "
                           "NO generes exploits ni payloads — solo identificación y análisis."),
            "service": {"host": host_ip, "port": port, "service": service,
                        "version": version, "product": product},
        }
        payload_phi = _payload_has_phi(host_ip, service, version, product)
        try:
            if self.cve_analyst is not None and not payload_phi:
                data = await self.cve_analyst(directive)
                return data if isinstance(data, dict) else dict(manual)
            if payload_phi:
                # PHI en el payload → cognición local, never-egress.
                prompt = (f"Vulnerability analysis (PHI-local). Service {service} {version} "
                          f"{product} on {host_ip}:{port}. JSON {{cve,cvss_score,severity,"
                          f"description,remediation}} only. No payloads.")
                return json.loads(await self.llm.query_fast(prompt))
            return dict(manual)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            return dict(manual)

    async def _cognition(
        self, task: str, prompt: str, *payload_parts: object,
        quality: bool = False, system: Optional[str] = None, fallback: str = "",
    ) -> str:
        """V17: cognición de TEXTO (risk summary, exploit-doc, recomendaciones) → Claude+CVP.

        Mismo enrutamiento payload-based que `_analyze_service`: infra/análisis (sin PHI) →
        cve_analyst inyectado (Claude+CVP); payload con PHI → local (never-egress); sin
        cerebro y sin PHI → ``fallback`` honesto (nunca el local que alucina). Solo análisis/
        documentación de técnica, NUNCA payloads (el system_prompt del caller lo refuerza).

        Returns:
            El texto de la cognición, o ``fallback``.
        """
        directive = {
            "task": task,
            "prompt": prompt,
            "system": system,
            "cvp": {
                "org": "5a188a34",
                "authorization": "CVP-approved dual-use; assessment scoped by contract_ref",
                "contract_ref": getattr(self.scope, "contract_ref", ""),
            },
            "constraint": ("Solo análisis / documentación de técnica. NO generes payloads ni "
                           "exploits ejecutables."),
        }
        phi = _payload_has_phi(prompt, *payload_parts)
        try:
            if self.cve_analyst is not None and not phi:
                out = await self.cve_analyst(directive)
                return out if isinstance(out, str) else json.dumps(out)
            if phi:
                q = self.llm.query_quality if quality else self.llm.query_fast
                return await q(prompt, system=system)
            return fallback
        except Exception:
            return fallback

    async def start(self) -> AssessmentSession:
        """Initialize a new assessment session. Validates scope and LLM availability.

        Returns:
            AssessmentSession in RECON phase

        Raises:
            RuntimeError: If Ollama API is unavailable
        """
        if not await self.llm.is_available():
            raise RuntimeError(
                f"Ollama not available at {self.llm.ollama_url}. "
                "Ensure localhost:11434 is running."
            )

        session = AssessmentSession(
            session_id=str(uuid.uuid4()),
            scope=self.scope,
            current_phase=AssessmentPhase.RECON,
        )
        self._persist_session(session)
        self.session = session
        return session

    async def run_recon(self, session: AssessmentSession) -> ReconResult:
        """Phase 1: Reconnaissance. Discovers hosts/ports/services in authorized scope.

        Uses nmap-compatible tooling (via subprocess or aris_dispatch MCP).
        Only scans hosts listed in scope.target_hosts — scope enforcement mandatory.

        Args:
            session: Assessment session context

        Returns:
            ReconResult with discovered hosts

        Raises:
            RuntimeError: If nmap not found or timeout
            PermissionError: If host outside authorized scope
        """
        # Validate all hosts before scanning
        for host in session.scope.target_hosts:
            if not self._validate_scope(host):
                raise PermissionError(
                    f"Host {host} outside authorized scope"
                )

        # Build nmap command
        cmd = [
            "nmap",
            "-sV",
            "-sC",
            "-T4",
            "--open",
            "-oX",
            "-",
        ] + session.scope.target_hosts

        try:
            # Execute nmap async
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=300.0
            )
        except FileNotFoundError:
            raise RuntimeError(
                "nmap not found. Install: brew install nmap"
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Recon timed out after 300s for scope: {session.scope.target_hosts}"
            )

        # Parse XML output
        hosts_discovered = []
        try:
            root = ET.fromstring(stdout.decode())
            for host_elem in root.findall("host"):
                # Extract IP address
                addr = host_elem.find("address[@addrtype='ipv4']")
                host_ip = addr.get("addr", "") if addr is not None else ""

                # Extract open ports
                ports = []
                for port_elem in host_elem.findall(".//port[@protocol='tcp']"):
                    portid = int(port_elem.get("portid", 0))
                    state = port_elem.find("state")
                    if state is not None and state.get("state") == "open":
                        service = port_elem.find("service")
                        ports.append(
                            {
                                "port": portid,
                                "service": (
                                    service.get("name", "")
                                    if service is not None
                                    else ""
                                ),
                                "version": (
                                    service.get("version", "")
                                    if service is not None
                                    else ""
                                ),
                                "product": (
                                    service.get("product", "")
                                    if service is not None
                                    else ""
                                ),
                            }
                        )

                if host_ip and ports:
                    hosts_discovered.append(
                        {"host": host_ip, "ports": ports}
                    )
        except ET.ParseError:
            # If XML parsing fails, return empty discoveries
            pass

        # Update session phase
        session.current_phase = AssessmentPhase.SCAN
        session.recon_result = ReconResult(
            hosts_discovered=hosts_discovered,
            raw_output=stdout.decode(),
            timestamp=datetime.now(UTC).isoformat(),
        )
        self._persist_session(session)

        return session.recon_result

    async def run_scan(
        self, session: AssessmentSession, recon: ReconResult
    ) -> ScanResult:
        """Phase 2: Vulnerability scan. Identifies CVEs and scores with CVSS.

        Uses healthcare vulnerability KB first for known patterns, then
        qwen35-analyst for novel services. Prioritizes findings by CVSS score.

        Args:
            session: Assessment session context
            recon: Phase 1 reconnaissance results

        Returns:
            ScanResult with prioritized vulnerabilities

        Raises:
            None - gracefully handles LLM failures
        """
        findings: list[VulnFinding] = []
        total_critical = 0
        total_high = 0

        # Initialize healthcare vulnerability KB
        kb = HealthcareVulnKB()

        # Analyze each discovered host/service
        for host_data in recon.hosts_discovered:
            host_ip = host_data.get("host", "")
            ports = host_data.get("ports", [])

            for port_info in ports:
                port = port_info.get("port", 0)
                service = port_info.get("service", "")
                version = port_info.get("version", "")
                product = port_info.get("product", "")

                # Try KB lookup first (port-based, then service-based)
                kb_match = kb.enrich_finding(host_ip, port, service, version)

                if kb_match:
                    # KB match found — use pre-loaded data
                    cve = kb_match.cve
                    cvss_score = kb_match.cvss_score
                    severity = kb_match.severity
                    description = kb_match.description
                    remediation = kb_match.remediation
                else:
                    # No KB match — V17: cognición de CVE vía Claude+CVP (cve_analyst
                    # inyectado), NO el local que alucina. Ver _analyze_service.
                    data = await self._analyze_service(host_ip, port, service, version, product)
                    cve = data.get("cve")
                    cvss_score = float(data.get("cvss_score", 0.0))
                    severity = data.get("severity", "info")
                    description = data.get("description", "")
                    remediation = data.get("remediation", "")

                # Create finding from KB or LLM
                finding = VulnFinding(
                    host=host_ip,
                    port=port,
                    service=service,
                    cve=cve,
                    cvss_score=cvss_score,
                    severity=severity,
                    description=description,
                    remediation=remediation,
                )
                findings.append(finding)

                # Count by severity
                if severity == "critical":
                    total_critical += 1
                elif severity == "high":
                    total_high += 1

        # Generate risk summary
        risk_summary_prompt = f"""Given these vulnerability counts: critical={total_critical}, high={total_high}
Write a 2-sentence executive risk summary for a healthcare organization."""

        risk_summary = await self._cognition(
            "risk_summary", risk_summary_prompt, str(total_critical), str(total_high),
            fallback=f"Identified {total_critical} critical and {total_high} high severity findings.",
        )

        # Create scan result
        result = ScanResult(
            findings=findings,
            total_critical=total_critical,
            total_high=total_high,
            risk_summary=risk_summary,
            timestamp=datetime.now(UTC).isoformat(),
        )

        session.current_phase = AssessmentPhase.SCAN
        session.scan_result = result
        self._persist_session(session)

        return result

    async def request_exploit_approval(
        self, session: AssessmentSession, scan: ScanResult
    ) -> HITLCheckpoint:
        """Phase 2→3 gate: Creates HITL checkpoint requiring human approval.

        Returns checkpoint in PENDING state. Caller must poll until APPROVED.
        Exploitation CANNOT proceed without approved checkpoint.

        Args:
            session: Assessment session context
            scan: Phase 2 scan results

        Returns:
            HITLCheckpoint with status=PENDING

        Raises:
            ValueError: If session has no scan results
        """
        if session.scan_result is None:
            raise ValueError("Cannot request approval without prior scan results")

        context = {
            "session_id": session.session_id,
            "client_id": session.scope.client_id,
            "engagement_id": session.scope.engagement_id,
            "critical_findings": session.scan_result.total_critical,
            "high_findings": session.scan_result.total_high,
            "risk_summary": session.scan_result.risk_summary,
        }

        checkpoint = self.hitl.create_checkpoint(
            session_id=session.session_id,
            phase=AssessmentPhase.EXPLOIT,
            context=context,
        )
        session.hitl_checkpoint = checkpoint
        self._persist_session(session)
        return checkpoint

    async def run_exploitation(
        self, session: AssessmentSession, checkpoint: HITLCheckpoint
    ) -> dict[str, Any]:
        """Phase 3: Controlled exploitation (HITL-approved only).

        Analyzes high/critical findings (CVSS >= 7.0) using qwen35-pentester model.
        Validates techniques against scope.authorized_techniques.
        Logs all actions to exploitation_audit table for complete audit trail.

        Args:
            session: Assessment session context
            checkpoint: HITL checkpoint (must be APPROVED)

        Returns:
            Dictionary with exploitation documentation results:
                - session_id: Assessment session identifier
                - engagement_id: Engagement identifier
                - checkpoint_id: HITL checkpoint identifier
                - approved_by: User who approved exploitation
                - findings_analyzed: Count of high/critical findings documented
                - techniques_documented: List of documented exploitation techniques
                - audit_logged: True if audit records persisted
                - note: Status note about CVP requirements

        Raises:
            PermissionError: If checkpoint is not APPROVED
            ValueError: If session missing scan results
        """
        if checkpoint.status != HITLStatus.APPROVED:
            raise PermissionError(
                f"Exploitation blocked: checkpoint {checkpoint.checkpoint_id} "
                f"status={checkpoint.status.value}. Human approval required."
            )

        if session.scan_result is None:
            raise ValueError(
                "Exploitation requires completed scan phase results"
            )

        # Filter to high/critical findings only (CVSS >= 7.0)
        findings_to_exploit = [
            f for f in session.scan_result.findings
            if f.cvss_score >= 7.0
        ]

        documented_techniques: list[tuple[VulnFinding, dict[str, Any]]] = []

        # System prompt for pentesting model
        system_prompt = """You are a penetration tester documenting exploitation techniques for authorized healthcare security assessments.
Describe exploitation approach for discovered vulnerabilities in structured format only.
Do NOT generate actual exploit code or payloads. Focus on technique description, prerequisites, and impact assessment.
All responses must be valid JSON."""

        # Analyze each finding with qwen35-pentester
        for finding in findings_to_exploit:
            prompt = f"""Authorized engagement: {session.scope.contract_ref}
Engagement ID: {session.scope.engagement_id}
Target: {finding.host}:{finding.port} ({finding.service})
CVE: {finding.cve or 'N/A'}
CVSS Score: {finding.cvss_score}
Severity: {finding.severity}
Description: {finding.description}

Document the exploitation technique for this vulnerability. Respond as valid JSON only with no additional text:
{{
  "technique": "brief technique name/approach",
  "prerequisites": ["list", "of", "required", "conditions"],
  "impact": "description of potential impact if successfully exploited",
  "difficulty": "easy|medium|hard",
  "mitre_ttp": "MITRE ATT&CK tactic/technique (e.g., T1234)"
}}"""

            try:
                # V17: técnica de exploit vía Claude+CVP (infra, no PHI); local solo si PHI.
                # El system_prompt (doc-only, no payloads) se preserva y viaja en la directiva.
                response = await self._cognition(
                    "exploit_doc", prompt, finding.host, finding.service, finding.cve or "",
                    system=system_prompt, fallback="",
                )
                # Parse JSON response
                try:
                    llm_result = json.loads(response)
                except (json.JSONDecodeError, ValueError):
                    # Fallback if no brain/invalid JSON — documentación manual, no fabricar.
                    llm_result = {
                        "technique": "Manual technique assessment required",
                        "prerequisites": ["expert review"],
                        "impact": "Requires manual analysis",
                        "difficulty": "hard",
                        "mitre_ttp": "T9999",
                    }
            except Exception as e:
                # If LLM call fails, create fallback entry
                llm_result = {
                    "technique": f"LLM analysis failed: {str(e)[:50]}",
                    "prerequisites": ["manual review"],
                    "impact": "Analysis unavailable",
                    "difficulty": "unknown",
                    "mitre_ttp": "N/A",
                }

            documented_techniques.append((finding, llm_result))

            # Log to audit table immediately
            self._log_exploitation_audit(
                session_id=session.session_id,
                engagement_id=session.scope.engagement_id,
                checkpoint_id=checkpoint.checkpoint_id,
                finding_host=finding.host,
                finding_port=finding.port,
                finding_cve=finding.cve,
                technique_used=llm_result.get("technique", ""),
                approved_by=checkpoint.resolver or "unknown",
            )

        # Update session phase
        session.current_phase = AssessmentPhase.REPORT
        self._persist_session(session)

        # Build response
        return {
            "session_id": session.session_id,
            "engagement_id": session.scope.engagement_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "approved_by": checkpoint.resolver,
            "findings_analyzed": len(findings_to_exploit),
            "techniques_documented": [
                {
                    "host": finding.host,
                    "port": finding.port,
                    "cve": finding.cve,
                    "cvss_score": finding.cvss_score,
                    "severity": finding.severity,
                    "technique": llm_result.get("technique", ""),
                    "mitre_ttp": llm_result.get("mitre_ttp", ""),
                    "difficulty": llm_result.get("difficulty", ""),
                    "impact": llm_result.get("impact", ""),
                }
                for finding, llm_result in documented_techniques
            ],
            "audit_logged": True,
            "note": (
                "Exploit technique documentation generated via local qwen35-pentester. "
                "Full active exploitation requires CVP-approved Claude API integration."
            ),
        }

    async def generate_report(self, session: AssessmentSession) -> str:
        """Phase 4: Generate bilingual (ES/EN) assessment report.

        Uses hermes3:70b for high-quality narrative generation.
        Output: Markdown report (path returned).

        Args:
            session: Assessment session context

        Returns:
            Path to generated report file

        Raises:
            ValueError: If session missing required recon/scan results
        """
        if session.recon_result is None or session.scan_result is None:
            raise ValueError(
                "Report generation requires completed recon and scan phases"
            )

        scan_result = session.scan_result
        scope = session.scope

        # Build findings table (critical and high only)
        critical_findings = [
            f for f in scan_result.findings if f.severity == "critical"
        ]
        high_findings = [
            f for f in scan_result.findings if f.severity == "high"
        ]

        critical_table = ""
        if critical_findings:
            critical_table = "### Critical Findings / Hallazgos Críticos\n\n"
            critical_table += (
                "| Host | Port | Service | CVE | CVSS | Description |\n"
            )
            critical_table += "|------|------|---------|-----|------|-------------|\n"
            for finding in critical_findings:
                critical_table += (
                    f"| {finding.host} | {finding.port} | {finding.service} | "
                    f"{finding.cve or 'N/A'} | {finding.cvss_score} | {finding.description} |\n"
                )

        high_table = ""
        if high_findings:
            high_table = "\n### High Findings / Hallazgos Altos\n\n"
            high_table += (
                "| Host | Port | Service | CVE | CVSS | Description |\n"
            )
            high_table += "|------|------|---------|-----|------|-------------|\n"
            for finding in high_findings:
                high_table += (
                    f"| {finding.host} | {finding.port} | {finding.service} | "
                    f"{finding.cve or 'N/A'} | {finding.cvss_score} | {finding.description} |\n"
                )

        # Generate recommendations using LLM
        top_findings_text = "\n".join(
            [
                f"- {f.host}:{f.port} ({f.service}): {f.description}"
                for f in scan_result.findings[:5]
            ]
        )

        recommendations_prompt = f"""You are a healthcare cybersecurity consultant writing recommendations for a penetration test report.
Client: {scope.client_id}, Target type: {scope.target_type.value}
Critical findings: {scan_result.total_critical}, High: {scan_result.total_high}

Top findings:
{top_findings_text}

Write 5 prioritized, actionable recommendations in both English and Spanish.
Format as numbered list with EN/ES pairs."""

        recommendations = await self._cognition(
            "recommendations", recommendations_prompt, quality=True,
            fallback="1. Establish a patching and vulnerability management program. / Establecer un programa de gestión de parches y vulnerabilidades.",
        )

        # Build report content
        report_content = f"""# Security Assessment Report / Reporte de Evaluación de Seguridad

## Client: {scope.client_id} | Engagement: {scope.engagement_id}
## Date: {datetime.now(UTC).isoformat()}

---

## Executive Summary / Resumen Ejecutivo

{scan_result.risk_summary}

## Scope / Alcance

- **Authorized hosts**: {", ".join(scope.target_hosts)}
- **Target type**: {scope.target_type.value}
- **Contract reference**: {scope.contract_ref}
- **Assessment phase**: Reconnaissance and Vulnerability Identification

## Findings / Hallazgos

{critical_table}{high_table}

## Recommendations / Recomendaciones

{recommendations}

## Methodology / Metodología

This assessment was conducted using industry-standard penetration testing methodology per PTES framework. Network reconnaissance was performed using nmap for service discovery and version detection. Identified services were analyzed for known vulnerabilities using CVSS scoring for risk prioritization.

Esta evaluación se realizó utilizando una metodología estándar de pruebas de penetración según el marco de trabajo PTES. Se realizó reconocimiento de red utilizando nmap para descubrimiento de servicios y detección de versiones. Los servicios identificados se analizaron para vulnerabilidades conocidas utilizando puntuación CVSS para la priorización de riesgos.

All activities conducted within authorized scope as defined by engagement contract.
Todas las actividades se realizaron dentro del alcance autorizado tal como se define en el contrato de compromiso.

---

*Report generated by ARIS4U V17 | HITL-reviewed*
*Classification: CONFIDENTIAL — Client Eyes Only*
"""

        # Save report to file
        reports_dir = (
            Path(__file__).parent.parent.parent / "data" / "reports"
        )
        reports_dir.mkdir(parents=True, exist_ok=True)
        report_path = reports_dir / (
            f"assessment_{scope.engagement_id}_"
            f"{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.md"
        )
        report_path.write_text(report_content, encoding="utf-8")

        # Update session
        session.report_path = str(report_path)
        session.completed_at = datetime.now(UTC).isoformat()
        self._persist_session(session)

        return str(report_path)

    async def generate_pdf_report(self, session: AssessmentSession) -> str:
        """Convert the Markdown assessment report to PDF using Pandoc.

        Implements fallback chain: wkhtmltopdf → weasyprint → latex default.

        Args:
            session: Completed assessment session with report_path set.

        Returns:
            Absolute path to the generated PDF file.

        Raises:
            ValueError: If session has no report_path (generate_report not called).
            ValueError: If Markdown file does not exist.
            RuntimeError: If pandoc is not installed or all conversion attempts fail.
        """
        # Verify report_path is set
        if session.report_path is None:
            raise ValueError(
                "No Markdown report found. Call generate_report() first."
            )

        # Verify Markdown file exists
        md_path = Path(session.report_path)
        if not md_path.exists():
            raise ValueError(
                f"Markdown report file not found: {md_path}"
            )

        # Construct PDF path (same directory, .pdf extension)
        pdf_path = md_path.with_suffix(".pdf")

        # Try conversion with fallback engines
        pdf_engines = [
            ("wkhtmltopdf", [
                "pandoc",
                str(md_path),
                "-o", str(pdf_path),
                "--pdf-engine=wkhtmltopdf",
                "-V", "geometry:margin=2cm",
                "-V", "fontsize=11pt",
                "--toc",
                "--highlight-style=tango",
            ]),
            ("weasyprint", [
                "pandoc",
                str(md_path),
                "-o", str(pdf_path),
                "--pdf-engine=weasyprint",
                "-V", "geometry:margin=2cm",
                "-V", "fontsize=11pt",
                "--toc",
                "--highlight-style=tango",
            ]),
            ("latex", [
                "pandoc",
                str(md_path),
                "-o", str(pdf_path),
                "-V", "geometry:margin=2cm",
                "-V", "fontsize=11pt",
                "--toc",
                "--highlight-style=tango",
            ]),
        ]

        last_error = None

        for engine_name, cmd in pdf_engines:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=60.0
                )

                # Check if conversion succeeded
                if proc.returncode == 0 and pdf_path.exists():
                    return str(pdf_path)

                # Capture error for next attempt
                last_error = stderr.decode()

            except FileNotFoundError:
                # Pandoc not found at all
                last_error = "pandoc not found"
            except asyncio.TimeoutError:
                last_error = f"PDF conversion timed out after 60s with {engine_name}"
            except Exception as e:
                last_error = str(e)

        # All engines failed
        error_msg = (
            f"PDF generation failed: {last_error[:500]}"
            if last_error
            else "PDF generation failed: unknown error"
        )
        raise RuntimeError(error_msg)

    def _log_exploitation_audit(
        self,
        session_id: str,
        engagement_id: str,
        checkpoint_id: str,
        finding_host: str,
        finding_port: int,
        finding_cve: Optional[str],
        technique_used: str,
        approved_by: str,
    ) -> None:
        """Log exploitation action to audit trail.

        Args:
            session_id: Parent session identifier
            engagement_id: Engagement identifier
            checkpoint_id: HITL checkpoint identifier
            finding_host: Target host IP/hostname
            finding_port: Target port number
            finding_cve: CVE identifier (optional)
            technique_used: Exploitation technique applied
            approved_by: User who approved exploitation
        """
        db_path = (
            self.hitl.db_path
            if hasattr(self, "hitl") and hasattr(self.hitl, "db_path")
            else SESSIONS_DB
        )

        timestamp = datetime.now(UTC).isoformat()
        outcome = "documented"

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO exploitation_audit
                (session_id, engagement_id, timestamp, finding_host, finding_port,
                 finding_cve, technique_used, checkpoint_id, approved_by, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    session_id,
                    engagement_id,
                    timestamp,
                    finding_host,
                    finding_port,
                    finding_cve,
                    technique_used,
                    checkpoint_id,
                    approved_by,
                    outcome,
                ),
            )
            conn.commit()

    def _validate_scope(self, host: str) -> bool:
        """Check if a host is within the authorized engagement scope.

        Supports exact IP matching and CIDR notation.

        Args:
            host: Target hostname or IP address

        Returns:
            True if host is in scope.target_hosts, False otherwise
        """
        try:
            # Try to parse as IP address
            target_ip = IPv4Address(host)

            for scope_entry in self.scope.target_hosts:
                # Try exact match
                if host == scope_entry:
                    return True

                # Try CIDR match
                try:
                    network = IPv4Network(scope_entry, strict=False)
                    if target_ip in network:
                        return True
                except (ValueError, AddressValueError):
                    # Not a valid network, skip
                    pass

            return False
        except (ValueError, AddressValueError):
            # Not a valid IP, try exact string match against target_hosts
            return host in self.scope.target_hosts

    def _persist_session(self, session: AssessmentSession) -> None:
        """Save session state to sessions.db for HITL persistence.

        Args:
            session: Session to persist
        """
        db_path = (
            self.hitl.db_path
            if hasattr(self, "hitl") and hasattr(self.hitl, "db_path")
            else SESSIONS_DB
        )

        # Create table if not exists
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assessment_sessions (
                    session_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    engagement_id TEXT NOT NULL,
                    current_phase TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    recon_json TEXT,
                    scan_json TEXT,
                    report_path TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    errors_json TEXT,
                    updated_at TEXT NOT NULL
                )
            """
            )

            # Serialize scope with enum conversion
            scope_dict = asdict(session.scope)
            scope_dict["target_type"] = session.scope.target_type.value
            scope_json = json.dumps(scope_dict)

            recon_json = (
                json.dumps(asdict(session.recon_result))
                if session.recon_result
                else None
            )

            scan_json = None
            if session.scan_result:
                scan_json = json.dumps(
                    {
                        "findings": [
                            asdict(f)
                            for f in session.scan_result.findings
                        ],
                        "total_critical": session.scan_result.total_critical,
                        "total_high": session.scan_result.total_high,
                        "risk_summary": session.scan_result.risk_summary,
                        "timestamp": session.scan_result.timestamp,
                    }
                )

            errors_json = json.dumps(session.errors)

            # Upsert session
            conn.execute(
                """
                INSERT OR REPLACE INTO assessment_sessions
                (session_id, client_id, engagement_id, current_phase, scope_json,
                 recon_json, scan_json, report_path, started_at, completed_at,
                 errors_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    session.session_id,
                    session.scope.client_id,
                    session.scope.engagement_id,
                    session.current_phase.value,
                    scope_json,
                    recon_json,
                    scan_json,
                    session.report_path,
                    session.started_at,
                    session.completed_at,
                    errors_json,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
