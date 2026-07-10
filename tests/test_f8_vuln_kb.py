"""Tests for F8 Healthcare Vulnerability Knowledge Base."""

from engine.v16.f8_vuln_kb import HEALTHCARE_VULN_KB, HealthcareVulnKB, KBEntry


class TestHealthcareVulnKB:
    """Test suite for HealthcareVulnKB class."""

    def setup_method(self) -> None:
        """Initialize KB for each test."""
        self.kb = HealthcareVulnKB()

    def test_lookup_by_port_rdp(self) -> None:
        """Test port lookup for RDP (3389)."""
        results = self.kb.lookup_by_port(3389)
        assert len(results) >= 1
        assert any("BlueKeep" in r.title or "RDP" in r.title for r in results)

    def test_lookup_by_port_dicom(self) -> None:
        """Test port lookup for DICOM (104)."""
        results = self.kb.lookup_by_port(104)
        assert len(results) >= 1
        assert any("DICOM" in r.title for r in results)

    def test_lookup_by_port_ssh(self) -> None:
        """Test port lookup for SSH (22)."""
        results = self.kb.lookup_by_port(22)
        assert len(results) >= 1
        assert any("SSH" in r.title for r in results)

    def test_lookup_by_port_no_results(self) -> None:
        """Test port lookup with no results."""
        results = self.kb.lookup_by_port(65432)
        assert len(results) == 0

    def test_lookup_by_service_ssh(self) -> None:
        """Test service lookup for SSH."""
        results = self.kb.lookup_by_service("ssh")
        assert len(results) >= 1
        titles = [r.title for r in results]
        assert any("ssh" in t.lower() for t in titles)

    def test_lookup_by_service_dicom(self) -> None:
        """Test service lookup for DICOM."""
        results = self.kb.lookup_by_service("dicom")
        assert len(results) >= 1
        assert all("DICOM" in r.title for r in results)

    def test_lookup_by_service_case_insensitive(self) -> None:
        """Test service lookup is case-insensitive."""
        results_lower = self.kb.lookup_by_service("dicom")
        results_upper = self.kb.lookup_by_service("DICOM")
        results_mixed = self.kb.lookup_by_service("DiCoM")
        assert len(results_lower) == len(results_upper) == len(results_mixed)

    def test_lookup_by_service_no_results(self) -> None:
        """Test service lookup with no results."""
        results = self.kb.lookup_by_service("nonexistent_service")
        assert len(results) == 0

    def test_lookup_by_cve_exact(self) -> None:
        """Test exact CVE lookup."""
        entry = self.kb.lookup_by_cve("CVE-2019-0708")
        assert entry is not None
        assert "BlueKeep" in entry.title

    def test_lookup_by_cve_case_insensitive(self) -> None:
        """Test CVE lookup is case-insensitive."""
        entry_upper = self.kb.lookup_by_cve("CVE-2019-0708")
        entry_lower = self.kb.lookup_by_cve("cve-2019-0708")
        assert entry_upper is not None
        assert entry_lower is not None
        assert entry_upper.cve == entry_lower.cve

    def test_lookup_by_cve_not_found(self) -> None:
        """Test CVE lookup not found."""
        entry = self.kb.lookup_by_cve("CVE-9999-99999")
        assert entry is None

    def test_enrich_finding_returns_highest_severity(self) -> None:
        """Test that enrich_finding returns highest CVSS score match."""
        entry = self.kb.enrich_finding("192.168.1.1", 3389, "rdp")
        assert entry is not None
        assert entry.cvss_score >= 7.0
        assert "RDP" in entry.title or "BlueKeep" in entry.title

    def test_enrich_finding_port_priority(self) -> None:
        """Test that port matching takes priority over service matching."""
        entry = self.kb.enrich_finding("192.168.1.1", 104, "dicom")
        assert entry is not None
        assert "DICOM" in entry.title

    def test_enrich_finding_service_fallback(self) -> None:
        """Test service-based matching when port not found."""
        entry = self.kb.enrich_finding("192.168.1.1", 65432, "mysql")
        assert entry is not None
        assert "MySQL" in entry.title

    def test_enrich_finding_unknown_returns_none(self) -> None:
        """Test enrich_finding returns None for unknown port and service."""
        entry = self.kb.enrich_finding("192.168.1.1", 12345, "unknown_service_xyz")
        assert entry is None

    def test_get_critical_entries_default_threshold(self) -> None:
        """Test get_critical_entries with default threshold (9.0)."""
        critical = self.kb.get_critical_entries()
        assert len(critical) >= 3
        assert all(e.cvss_score >= 9.0 for e in critical)
        # Verify sorted in descending order
        assert critical[0].cvss_score >= critical[-1].cvss_score

    def test_get_critical_entries_custom_threshold(self) -> None:
        """Test get_critical_entries with custom threshold."""
        critical = self.kb.get_critical_entries(min_cvss=8.0)
        assert len(critical) >= 5
        assert all(e.cvss_score >= 8.0 for e in critical)

    def test_get_critical_entries_sorted_descending(self) -> None:
        """Test that critical entries are sorted in descending order."""
        critical = self.kb.get_critical_entries(min_cvss=8.0)
        for i in range(len(critical) - 1):
            assert critical[i].cvss_score >= critical[i + 1].cvss_score

    def test_stats_returns_correct_counts(self) -> None:
        """Test stats() returns correct counts."""
        stats = self.kb.stats()
        assert stats["total_entries"] == len(HEALTHCARE_VULN_KB)
        assert stats["critical"] >= 1
        assert stats["high"] >= 1
        assert stats["with_cve"] >= 5
        assert stats["avg_cvss"] > 0.0
        assert isinstance(stats["avg_cvss"], float)

    def test_stats_critical_count(self) -> None:
        """Test that stats critical count matches actual entries."""
        stats = self.kb.stats()
        actual_critical = sum(1 for e in HEALTHCARE_VULN_KB if e.severity == "critical")
        assert stats["critical"] == actual_critical

    def test_all_entries_have_required_fields(self) -> None:
        """Test that all KB entries have required fields."""
        for entry in HEALTHCARE_VULN_KB:
            assert entry.title, "Entry missing title"
            assert entry.cvss_score > 0, "Entry missing CVSS score"
            assert entry.severity in [
                "critical",
                "high",
                "medium",
                "low",
                "info",
            ], f"Invalid severity: {entry.severity}"
            assert entry.description, "Entry missing description"
            assert entry.remediation, "Entry missing remediation"
            assert entry.healthcare_impact, "Entry missing healthcare_impact"
            assert len(entry.affected_products) > 0, "Entry missing affected_products"
            assert len(entry.ports) > 0, "Entry missing ports"
            assert len(entry.services) > 0, "Entry missing services"

    def test_kb_entry_dataclass(self) -> None:
        """Test KBEntry dataclass creation and fields."""
        entry = KBEntry(
            cve="CVE-2021-44228",
            title="Test Entry",
            affected_products=["Product1"],
            ports=[443],
            services=["https"],
            cvss_score=9.5,
            severity="critical",
            description="Test description",
            remediation="Test remediation",
            healthcare_impact="Test impact",
        )
        assert entry.cve == "CVE-2021-44228"
        assert entry.title == "Test Entry"
        assert entry.cvss_score == 9.5

    def test_kb_entry_optional_fields(self) -> None:
        """Test KBEntry with optional fields."""
        entry = KBEntry(
            cve=None,
            title="Test",
            affected_products=["Product"],
            ports=[80],
            services=["http"],
            cvss_score=5.0,
            severity="medium",
            description="Desc",
            remediation="Fix",
            healthcare_impact="Impact",
            mitre_ttp="T1234",
            references=["http://example.com"],
        )
        assert entry.cve is None
        assert entry.mitre_ttp == "T1234"
        assert len(entry.references) == 1

    def test_kb_initialization_with_custom_entries(self) -> None:
        """Test KB initialization with custom entries."""
        custom_entry = KBEntry(
            cve="CVE-9999-9999",
            title="Custom Test",
            affected_products=["Test Product"],
            ports=[9999],
            services=["test"],
            cvss_score=7.5,
            severity="high",
            description="Custom test entry",
            remediation="Custom fix",
            healthcare_impact="Custom impact",
        )
        custom_kb = HealthcareVulnKB(entries=[custom_entry])
        assert custom_kb.stats()["total_entries"] == 1
        assert custom_kb.lookup_by_port(9999)[0].title == "Custom Test"

    def test_enrich_finding_with_version(self) -> None:
        """Test enrich_finding with version parameter (even if not used)."""
        entry = self.kb.enrich_finding("192.168.1.1", 3389, "rdp", version="6.1")
        assert entry is not None

    def test_multiple_port_matches_highest_cvss(self) -> None:
        """Test that multiple port matches return highest CVSS."""
        # Port 443 is in multiple entries, should return highest CVSS
        results = self.kb.lookup_by_port(443)
        if len(results) > 1:
            highest = self.kb.enrich_finding("192.168.1.1", 443, "https")
            scores = [e.cvss_score for e in results]
            assert highest.cvss_score == max(scores)  # type: ignore[union-attr]  # enrich_finding returns None only when no port match; guarded by len(results)>1

    def test_kb_completeness(self) -> None:
        """Test that KB has minimum expected entries."""
        assert len(HEALTHCARE_VULN_KB) >= 10, "KB should have at least 10 entries"
        assert self.kb.stats()["critical"] >= 5, "KB should have at least 5 critical entries"

    def test_no_duplicate_cves(self) -> None:
        """Test that KB has no duplicate CVE entries."""
        cves = [e.cve for e in HEALTHCARE_VULN_KB if e.cve]
        assert len(cves) == len(set(cves)), "KB contains duplicate CVE entries"
