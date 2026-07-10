"""
F8.VULN_KB — Healthcare Vulnerability Knowledge Base.

Pre-loaded CVE and vulnerability data for healthcare-specific systems.
Used by F8 AssessmentWorkflow.run_scan() to enrich LLM analysis with
known healthcare attack patterns.

Systems covered:
- Epic EMR, Cerner, Allscripts (EHR systems)
- DICOM/PACS imaging systems
- HL7 MLLP messaging
- Medical IoT (Philips, GE, Siemens devices)
- VPN/remote access (common in post-COVID healthcare)
- Pharmacy management systems
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class KBEntry:
    """A single vulnerability knowledge base entry."""

    cve: Optional[str]
    title: str
    affected_products: list[str]
    ports: list[int]
    services: list[str]
    cvss_score: float
    severity: str
    description: str
    remediation: str
    healthcare_impact: str
    mitre_ttp: Optional[str] = None
    references: list[str] = field(default_factory=list)


# Healthcare-specific vulnerability knowledge base
HEALTHCARE_VULN_KB: list[KBEntry] = [
    # === EHR/EMR Systems ===
    KBEntry(
        cve="CVE-2021-44228",
        title="Log4Shell in Healthcare EHR Systems",
        affected_products=["Epic", "Cerner", "Allscripts", "Java-based EHR"],
        ports=[8080, 8443, 443],
        services=["http", "https"],
        cvss_score=10.0,
        severity="critical",
        description="Log4j JNDI injection allowing RCE in Java-based EHR systems",
        remediation="Update Log4j to 2.17.1+. Apply vendor patches immediately.",
        healthcare_impact="Full EMR compromise, patient data exfiltration, ransomware deployment",
        mitre_ttp="T1190",
        references=["https://nvd.nist.gov/vuln/detail/CVE-2021-44228"],
    ),
    KBEntry(
        cve="CVE-2019-0708",
        title="BlueKeep RDP Vulnerability",
        affected_products=["Windows RDP", "Windows Server 2008", "Windows 7"],
        ports=[3389],
        services=["rdp", "ms-wbt-server"],
        cvss_score=9.8,
        severity="critical",
        description="Pre-auth RCE in Windows RDP service — wormable",
        remediation="Patch MS19-0708. Disable RDP if unused. Require NLA.",
        healthcare_impact="Ransomware entry point — common in healthcare breaches 2019-2023",
        mitre_ttp="T1210",
    ),
    KBEntry(
        cve="CVE-2016-6515",
        title="OpenSSH 7.x DoS",
        affected_products=["OpenSSH < 7.4"],
        ports=[22],
        services=["ssh"],
        cvss_score=7.8,
        severity="high",
        description="Unauthenticated DoS via crafted packets in OpenSSH 7.2p2",
        remediation="Upgrade to OpenSSH 8.x+. Implement fail2ban.",
        healthcare_impact="Clinical system downtime, delayed patient care",
        mitre_ttp="T1499",
    ),
    # === DICOM/PACS Systems ===
    KBEntry(
        cve=None,
        title="DICOM Unauthenticated Access",
        affected_products=["Orthanc", "DCM4CHEE", "Horos", "OsiriX"],
        ports=[104, 11112, 4242],
        services=["dicom"],
        cvss_score=9.1,
        severity="critical",
        description="DICOM standard lacks authentication by default. Many PACS servers expose patient imaging data without credentials.",
        remediation="Implement TLS-DICOM (port 2762). Add VPN/firewall to restrict DICOM access. Enable DICOM audit logging.",
        healthcare_impact="Patient imaging data (PHI) fully accessible. HIPAA violation.",
        mitre_ttp="T1530",
    ),
    KBEntry(
        cve="CVE-2023-43177",
        title="CrushFTP Unauthenticated RCE (Healthcare File Transfer)",
        affected_products=["CrushFTP < 10.5.2"],
        ports=[443, 8080, 2100],
        services=["https", "ftp"],
        cvss_score=9.8,
        severity="critical",
        description="Unauthenticated RCE in CrushFTP — commonly used for HIPAA-compliant file transfer in healthcare",
        remediation="Update to CrushFTP 10.5.2+. Restrict access to authorized IPs.",
        healthcare_impact="Medical file transfer server compromise, PHI exfiltration",
        mitre_ttp="T1190",
    ),
    # === Medical IoT ===
    KBEntry(
        cve=None,
        title="Medical Device Default Credentials",
        affected_products=["Philips IVUS", "GE Healthcare devices", "Siemens Healthineers", "Baxter pumps"],
        ports=[80, 443, 22, 23],
        services=["http", "https", "ssh", "telnet"],
        cvss_score=9.0,
        severity="critical",
        description="Medical IoT devices shipped with default credentials (admin/admin, service/service). Rarely changed in clinical environments.",
        remediation="IMMEDIATE: Change all default credentials. Segment medical IoT on isolated VLAN. Disable telnet.",
        healthcare_impact="Patient safety risk — device manipulation possible. PHI exposure.",
        mitre_ttp="T1078",
    ),
    KBEntry(
        cve=None,
        title="HL7 MLLP Unauthenticated Message Injection",
        affected_products=["HL7 MLLP servers", "ADT systems", "Lab Information Systems"],
        ports=[2575, 2576, 6661],
        services=["mllp", "hl7"],
        cvss_score=8.5,
        severity="high",
        description="HL7 MLLP (Minimal Lower Layer Protocol) has no authentication by default. Attackers can inject false ADT/lab messages.",
        remediation="Implement TLS-wrapped MLLP. Firewall HL7 ports. Validate message source IPs.",
        healthcare_impact="False patient records, medication errors, lab result tampering",
        mitre_ttp="T1565",
    ),
    # === VPN/Remote Access ===
    KBEntry(
        cve="CVE-2023-46747",
        title="F5 BIG-IP Auth Bypass (Healthcare VPN)",
        affected_products=["F5 BIG-IP < 17.1.0.3"],
        ports=[443, 8443],
        services=["https"],
        cvss_score=9.8,
        severity="critical",
        description="Unauthenticated RCE via iControl REST API bypass in F5 BIG-IP — widely used in healthcare VPN infrastructure",
        remediation="Patch to F5 BIG-IP 17.1.0.3+. Restrict management interface access.",
        healthcare_impact="VPN gateway compromise — full network access to clinical systems",
        mitre_ttp="T1190",
    ),
    KBEntry(
        cve="CVE-2024-3400",
        title="PAN-OS GlobalProtect Command Injection",
        affected_products=["Palo Alto PAN-OS < 11.1.2-h3"],
        ports=[443],
        services=["https"],
        cvss_score=10.0,
        severity="critical",
        description="Pre-auth command injection in GlobalProtect VPN — exploited in healthcare sector",
        remediation="Patch to PAN-OS 11.1.2-h3+. Enable Threat Prevention.",
        healthcare_impact="Network perimeter breach, clinical data center access",
        mitre_ttp="T1190",
    ),
    # === Database / Backend ===
    KBEntry(
        cve=None,
        title="MySQL Exposed Without Auth (Healthcare Databases)",
        affected_products=["MySQL 5.x", "MariaDB"],
        ports=[3306],
        services=["mysql"],
        cvss_score=9.4,
        severity="critical",
        description="Healthcare databases exposed on port 3306 without firewall — common in smaller clinics",
        remediation="Bind MySQL to localhost. Firewall port 3306. Require SSL for remote connections.",
        healthcare_impact="Complete patient database compromise, PHI exfiltration",
        mitre_ttp="T1530",
    ),
]


class HealthcareVulnKB:
    """Query interface for the healthcare vulnerability knowledge base."""

    def __init__(self, entries: list[KBEntry] = HEALTHCARE_VULN_KB) -> None:
        """Initialize the vulnerability knowledge base.

        Args:
            entries: List of KB entries (default: HEALTHCARE_VULN_KB)
        """
        self._entries = entries

    def lookup_by_port(self, port: int) -> list[KBEntry]:
        """Find KB entries matching a specific port number.

        Args:
            port: Port number to search for

        Returns:
            List of matching KB entries
        """
        return [e for e in self._entries if port in e.ports]

    def lookup_by_service(self, service: str) -> list[KBEntry]:
        """Find KB entries matching a service name (case-insensitive).

        Args:
            service: Service name to search for

        Returns:
            List of matching KB entries
        """
        service_lower = service.lower()
        return [
            e
            for e in self._entries
            if any(s.lower() == service_lower for s in e.services)
        ]

    def lookup_by_cve(self, cve: str) -> Optional[KBEntry]:
        """Find exact CVE entry.

        Args:
            cve: CVE identifier (e.g., CVE-2021-44228)

        Returns:
            KBEntry if found, None otherwise
        """
        for entry in self._entries:
            if entry.cve and entry.cve.upper() == cve.upper():
                return entry
        return None

    def enrich_finding(
        self, host: str, port: int, service: str, version: str = ""
    ) -> Optional[KBEntry]:
        """Find the most relevant KB entry for a discovered service.

        Matches by port+service (exact match), then port alone, then service alone.
        Returns highest-severity match.

        Args:
            host: Target host IP/hostname
            port: Target port number
            service: Service name/identifier
            version: Service version (optional, not used in matching)

        Returns:
            Most relevant KBEntry if found, None otherwise
        """
        service_lower = service.lower()
        candidates: list[KBEntry] = []

        # 1. Prefer exact port+service matches
        port_matches = self.lookup_by_port(port)
        for entry in port_matches:
            if any(s.lower() == service_lower for s in entry.services):
                candidates.append(entry)

        # 2. If no exact match, try port alone
        if not candidates:
            candidates.extend(port_matches)

        # 3. If still no match, try service alone
        if not candidates:
            candidates.extend(self.lookup_by_service(service))

        if not candidates:
            return None
        return max(candidates, key=lambda e: e.cvss_score)

    def get_critical_entries(self, min_cvss: float = 9.0) -> list[KBEntry]:
        """Return all entries above CVSS threshold, sorted by score descending.

        Args:
            min_cvss: Minimum CVSS score to include (default 9.0)

        Returns:
            Sorted list of critical entries
        """
        return sorted(
            [e for e in self._entries if e.cvss_score >= min_cvss],
            key=lambda e: e.cvss_score,
            reverse=True,
        )

    def stats(self) -> dict[str, int | float]:
        """Return KB statistics.

        Returns:
            Dictionary with counts and averages
        """
        return {
            "total_entries": len(self._entries),
            "critical": sum(1 for e in self._entries if e.severity == "critical"),
            "high": sum(1 for e in self._entries if e.severity == "high"),
            "with_cve": sum(1 for e in self._entries if e.cve),
            "avg_cvss": round(
                sum(e.cvss_score for e in self._entries)
                / max(len(self._entries), 1),
                1,
            ),
        }
