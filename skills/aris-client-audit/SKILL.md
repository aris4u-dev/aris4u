---
name: aris-client-audit
description: |
  Parametrized code audit for 3rd-party and client repositories.
  Invokes aris_dialectic (Builder + Reviewer + Security roles) to review code for security risks,
  tech debt, and compliance gaps. Outputs SARIF report + severity-tagged findings.
  When to use: Auditing client codebases, risk assessments, compliance reviews.
  Example: `/aris-client-audit acme-corp ~/projects/03-clients/acme-corp security`
---

## When to Use

- **Client Code Review**: Audit 3rd-party repositories for security, tech debt, or compliance
- **Risk Assessment**: Identify CRITICAL/HIGH vulnerabilities before deployment
- **Compliance Validation**: Check for missing patterns (encryption, auth validation, RLS, logging)
- **Engagement Audit**: Validate client deliverables against standards

## Prerequisites

- Repo path must be readable (local or remote clone)
- aris_dialectic MCP tool available (Builder + Reviewer + Security roles)
- Client must be known (any name accepted; use `{custom}` for first-time clients)
- bash, find, grep available for codebase introspection

## Usage

```bash
/aris-client-audit <client_name> <repo_path> [<scope>]
```

**Parameters:**
- `client_name`: acme | my-client | {custom}
- `repo_path`: Absolute path to repository (~/projects/03-clients/your-client)
- `scope` (optional): security | tech-debt | compliance | all (default: all)

**Examples:**

```bash
# Full audit (all scopes)
/aris-client-audit acme ~/projects/03-clients/acme

# Security-focused audit
/aris-client-audit acme ~/projects/03-clients/acme security

# Tech debt audit
/aris-client-audit my-client ~/projects/03-clients/my-client/service tech-debt

# Compliance audit (HIPAA, SOC2, RLS patterns)
/aris-client-audit my-client ~/projects/03-clients/my-client compliance
```

## Execution Flow

1. **Scope Detection**: Parse repo for file types (*.py, *.java, *.tsx, *.sql, Dockerfile, etc.)
2. **Invocation**: Call `aris_dialectic(task, file_path)` for each module/layer:
   - **Builder role**: Scan for missing patterns, incomplete implementations
   - **Reviewer role**: Check code quality, duplication, architectural violations
   - **Security role**: Flag hardcoded secrets, auth bypass, injection risks, crypto issues
3. **Classification**: Tag each finding by severity (CRITICAL, HIGH, MEDIUM, LOW, INFO)
4. **Synthesis**: Generate SARIF report + markdown summary
5. **Decision Locking**: Call `aris_ingest()` to lock audit findings with client_id

## Output

### SARIF Report
- Path: `~/Desktop/audits/{ClientName}_{timestamp}.sarif`
- Format: SARIF 2.1.0 (IDE-compatible)
- Fields: rule.id (severity), message.text, location.physicalLocation.artifactLocation.uri

### Markdown Report
- Path: `~/Desktop/audits/{ClientName}_{timestamp}.md`
- Sections:
  - **Executive Summary**: Critical findings count, estimated remediation effort
  - **Findings by Severity**: CRITICAL → HIGH → MEDIUM → LOW → INFO
  - **Tech Debt Estimate**: Lines needing refactor, modules at risk
  - **Compliance Gaps**: Missing patterns (RLS, encryption, auth, validation)
  - **Actionable Recommendations**: 3–5 prioritized recs with effort estimates

### Locked Decisions
- Stored in sessions.db: `decisions` table with client_id = {client_name}
- Enables auto-recall on next audit or future decisions via `aris_recall_client()`

## Quality Gates

**Audit Success:**
- ✅ All findings tagged with severity (no unclassified)
- ✅ CRITICAL/HIGH findings include remediation rationale
- ✅ Report includes compliance gaps (if scope=compliance or scope=all)
- ✅ Recommendations are actionable (not generic)
- ✅ client_id locked in sessions.db for engagement tracking

**Failure Conditions:**
- ✅ Repo not found → abort with clear error
- ✅ aris_dialectic unavailable → fallback to local grep-based audit (reduced fidelity)
- ✅ Timeout (>10min) → emit partial report + note incomplete scans

## Scope Details

### security
- Hardcoded secrets (API keys, passwords, tokens)
- SQL injection, XSS, CSRF, IDOR patterns
- Crypto: weak ciphers, missing encryption, MD5/SHA1 usage
- Authentication: weak JWT, missing validation, privilege escalation
- API security: CORS misconfig, missing rate limits, exposed endpoints
- PHI/PII leaks: logging, API responses, database plaintext storage

### tech-debt
- Duplication (functions >200 LOC, copy-paste patterns)
- Missing tests (coverage <70%)
- Deprecated dependencies (outdated versions, EOL libraries)
- Dead code (unused modules, functions, imports)
- Architectural violations (circular imports, layer mixing, tight coupling)

### compliance
- HIPAA: encryption in transit/rest, audit logging, access controls
- SOC2: data retention, change logs, incident response
- RLS (Supabase/PostgreSQL): missing policies, overpermissioned tables
- Data handling: PII classification, retention policies, anonymization
- Governance: code review process, deployment approval, secrets management

## Notes

- **Scope Filtering**: If `scope` parameter omitted, audit runs all three (security + tech-debt + compliance). Use `security` alone to speed up (5–10 min vs 15–20 min for full audit).
- **Large Repos**: If repo >10K files, automatically sample high-risk files (controllers, auth handlers, data models) to avoid timeout.
- **Client Auto-Detect**: If cwd inside ~/projects/03-clients/{client}/, automatically infers client_name; explicit parameter overrides.
- **aris_dialectic Pattern**: Each file review invokes aris_dialectic once per role (Builder, Reviewer, Security) = 3 MCP calls/file. Parallelization across files reduces overhead.
- **Previous Findings**: Query sessions.db `SELECT * FROM decisions WHERE client_id=? AND domain='audit'` to avoid re-flagging known issues (unless scope changes).
- **Engagement Tracking**: Each finding is inserted as a row in the `engagement_findings` table (columns: `engagement_id`, `title`, `severity`, `category`, `description`, `remediation`, `cvss`, `status`). The per-severity rollups live on the parent `engagements` table as `findings_critical`, `findings_high`, `findings_medium`, `findings_low`, `findings_info` (NOT `critical_count`/`high_count`, which do not exist anywhere). Compute them from the findings rows: `SELECT severity, COUNT(*) FROM engagement_findings WHERE engagement_id=? GROUP BY severity` and write the totals back to the matching `engagements` row.

## Integration with ARIS4U Workflow

```
cd ~/projects/03-clients/your-client/
/aris-client-audit your-client . security
  → runs aris_dialectic on client code
  → locks findings in sessions.db (client_id='your-client')
  → outputs ~/Desktop/audits/your-client_<timestamp>.sarif + .md
  → future `/aris-memory-audit your-client security` auto-recalls these findings
```

---

**Version:** WS4-v1.0  
**Status:** Production  
**Last Updated:** 2026-05-31
