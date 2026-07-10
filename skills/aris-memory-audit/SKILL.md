---
name: aris-memory-audit
description: |
  Audit ARIS4U session memory (sessions.db) for per-client locked decisions, guards, and compliance.
  Identifies contradictions, staleness, compliance gaps (missing security/HIPAA/SOC2 decisions).
  Outputs: decision list, guard list, conflict report, compliance flags.
  When to use: Verify client decisions are consistent, check compliance coverage, detect stale patterns.
  Example: `/aris-memory-audit my-client security`
---

## When to Use

- **Client Decision Review**: List all locked decisions for a client (e.g., acme auth decisions)
- **Compliance Validation**: Verify sufficient security/HIPAA/SOC2 decisions are locked
- **Contradiction Detection**: Flag decisions that contradict each other (Guard A says "use JWT", Decision says "use OAuth")
- **Staleness Check**: Identify decisions not updated in 30+ days (may be outdated)
- **Engagement Tracking**: Verify decisions were locked during client engagement (not orphaned)

## Prerequisites

- sessions.db accessible at `~/projects/aris4u/data/sessions.db` (default) or `/path/to/sessions.db`. (Note: `~/.claude-mem/` holds `claude-mem.db`, the narrative memory DB — NOT `sessions.db`.)
- Client must exist in decisions/guards tables (client_id column populated)
- sqlite3 CLI available (or Python sqlite3 module)
- Optional: domain filter (auth|database|security|compliance|api|infra)

## Usage

```bash
/aris-memory-audit <client_name> [--domain <domain>] [--check-compliance] [--days-old N]
```

**Parameters:**
- `client_name`: acme | my-client | {custom}
- `--domain` (optional): auth | database | security | compliance | api | infra | all (default: all)
- `--check-compliance` (optional): Flag missing HIPAA/SOC2/GDPR decisions
- `--days-old N` (optional): Flag decisions older than N days (default: 30)

**Examples:**

```bash
# List all decisions for a client
/aris-memory-audit my-client

# Security-only decisions
/aris-memory-audit my-client --domain security

# Auth decisions with staleness check (>15 days old)
/aris-memory-audit my-client --domain auth --days-old 15

# Compliance check: identify missing HIPAA/SOC2 decisions
/aris-memory-audit my-client --check-compliance

# All decisions + compliance gaps
/aris-memory-audit my-client --check-compliance
```

## Execution Flow

1. **Query Decisions**
   - Execute: `SELECT id, decision, domain, created_at, session_ref FROM decisions WHERE client_id=? AND locked=1 ORDER BY created_at DESC LIMIT 100`
   - Filter by domain if specified

2. **Query Guards**
   - Execute: `SELECT pattern, prevention, severity, created_at FROM guards WHERE client_id=? ORDER BY created_at DESC LIMIT 100`
   - Identify guards specific to this client

3. **Detect Contradictions**
   - Parse decision text + guard patterns
   - Flag if Decision A says "require encryption" but Guard B says "allow plaintext"
   - Example: Decision: "No plaintext secrets" vs Guard: "Log all environment variables" = conflict

4. **Staleness Check** (if --days-old specified)
   - For each decision: check `CURRENT_TIMESTAMP - created_at > (days_old * 86400)`
   - Flag stale decisions (e.g., "JWT validation (2026-03-01, 91 days old)")
   - Suggest re-audit or confirmation

5. **Compliance Check** (if --check-compliance)
   - Count decisions in domains: security, auth, database, api
   - Check for specific patterns: "encryption", "RLS", "audit logging", "access control"
   - Flag missing HIPAA (encryption, audit logs, access control) / SOC2 (change logs, incident response) / GDPR (data retention)

6. **Engagement Correlation**
   - The `engagement_findings` table is keyed by `engagement_id` (FK to `engagements.id`), NOT by `client_id`. To correlate, first resolve the client's engagement ids via the `engagements` table, then: `SELECT COUNT(*) FROM engagement_findings WHERE engagement_id IN (<client engagement ids>)`
   - Verify locked decisions align with engagement findings
   - Flag orphaned decisions (locked but not tied to any engagement)

## Output

### Decisions Report
- Path: `~/Desktop/audits/memory_{ClientName}_{timestamp}.md`
- Format: List of locked decisions with metadata
  ```markdown
  ## <Client> Locked Decisions (23 total)

  ### Security Domain (8 decisions)
  1. [session_ref: REF-2026-04-10] JWT validation required on all endpoints
     Rationale: Prevent unauthorized access
     Created: 2026-04-10 (51 days old)
     Stale (>30 days)

  2. [session_ref: REF-2026-05-15] No plaintext secrets in logs
     Created: 2026-05-15 (16 days old)

  ### Auth Domain (5 decisions)
  1. [session_ref: REF-2026-04-20] OAuth 2.0 required for 3rd-party integrations
     Created: 2026-04-20 (41 days old)

  ### Database Domain (4 decisions)
  ...
  ```

### Guards Report
- Path: `~/Desktop/audits/memory_{ClientName}_{timestamp}.guards`
- Format: Client-specific guards and prevention rules
  ```
  === <Client> Guards (12 total) ===

  [severity: critical]
  Pattern: hardcoded API keys
  Prevention: reject commit if pattern found in .py, .env, .tf
  Created: 2026-04-15

  [severity: high]
  Pattern: log.*password|secret|token
  Prevention: warn if pattern found in logging statements
  ```

### Contradiction Report
- Path: `~/Desktop/audits/memory_{ClientName}_{timestamp}.conflicts`
- Format: Flagged contradictions
  ```
  === <Client> Contradictions ===

  CONTRADICTION:
    Decision: "All environment variables logged for audit"
    Guard: "No plaintext secrets in logs"
    Risk: Secrets may leak if .env contains API keys
    Recommendation: Rotate secrets + update decision to "Log sanitized environment variables"

  No other contradictions detected.
  ```

### Compliance Report (if --check-compliance)
- Path: `~/Desktop/audits/memory_{ClientName}_{timestamp}.compliance`
- Format: Coverage matrix
  ```
  === <Client> Compliance Coverage ===

  HIPAA Requirements:
    + Encryption in transit (decision locked 2026-05-20)
    + Encryption at rest (decision locked 2026-05-18)
    + Audit logging (decision locked 2026-05-15)
    + Access control (decision locked 2026-04-20)

  SOC2 Requirements:
    + Change management (decision locked 2026-05-10)
    - Incident response (NO DECISION FOUND)
    + Availability/uptime (decision locked 2026-05-12)
    ~ Data retention (decision exists but STALE, 60 days old)

  GDPR Requirements:
    + Data retention policy (decision locked 2026-05-01)
    + Consent management (decision locked 2026-05-05)
    - Right to deletion (NO DECISION FOUND)

  Summary: 10/14 required decisions locked. 2 missing. 1 stale.
  Recommendation: Lock "Incident response SLA" and "Right to deletion" before next compliance audit.
  ```

### Summary (stdout)
```
<Client> Memory Audit Results:
  Decisions: 23 locked (domain: security=8, auth=5, database=4, api=6)
  Guards: 12 (critical=2, high=4, medium=6)
  Contradictions: 1 flagged
  Stale decisions (>30 days): 3 (JWT validation, OAuth 2.0, Admin access)
  Compliance: 10/14 HIPAA/SOC2/GDPR decisions (missing: incident response, right to deletion)

Reports written to ~/Desktop/audits/memory_<Client>_<timestamp>.*
```

## Quality Gates

**Audit Completeness:**
- ✅ All decisions retrieved (COUNT query returns expected number)
- ✅ Contradictions flagged (if any)
- ✅ Staleness detected (>30 days flagged)
- ✅ Compliance gaps identified (if --check-compliance)
- ✅ Reports written to ~/Desktop/audits/

**Failure Conditions:**
- ✅ Client not found → return 0 decisions + recommend running `/aris-audit` first
- ✅ sessions.db locked → emit error + suggest retry
- ✅ Contradiction logic error → fallback to simple list (non-blocking)

## Domain Definitions

- **auth**: JWT, OAuth, SAML, MFA, password policy, session management
- **database**: Encryption, RLS, audit logging, backups, migrations
- **security**: Secrets management, key rotation, vulnerability disclosure
- **compliance**: HIPAA, SOC2, GDPR, PCI-DSS, legal holds
- **api**: Rate limiting, CORS, input validation, versioning
- **infra**: Docker, Kubernetes, networking, secrets in infra-as-code

## Notes

- **Query Scope**: Only retrieves decisions locked by aris_ingest() (locked=1). Transient decisions are not included.
- **Client Auto-Detect**: If cwd inside ~/projects/03-clients/{client}/, automatically infers client_name; explicit parameter overrides.
- **Stale Definition**: Decisions >30 days old flagged as stale (configurable with --days-old).
- **Contradiction Detection**: Simple text matching; may miss logical contradictions (review conflicts manually).
- **Compliance Baseline**: HIPAA/SOC2/GDPR requirements hardcoded; can be extended in sessions.db `compliance_requirements` table.
- **Engagement Correlation**: Decisions without matching engagement_findings are "orphaned" (flagged for cleanup).

## Integration with ARIS4U Workflow

```
cd ~/projects/03-clients/your-client/
/aris-memory-audit your-client security --check-compliance
  → queries sessions.db for your-client (client_id='your-client')
  → filters by domain='security' (if specified)
  → detects contradictions, staleness, compliance gaps
  → outputs reports:
    - memory_<client>_*.md (decision list)
    - memory_<client>_*.guards (guards list)
    - memory_<client>_*.conflicts (contradiction report)
    - memory_<client>_*.compliance (compliance matrix)
  → enables quick verification before client hand-off
```

---

**Version:** WS4-v1.0  
**Status:** Production  
**Last Updated:** 2026-05-31
