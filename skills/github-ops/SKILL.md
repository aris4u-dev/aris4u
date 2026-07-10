---
name: github-ops
description: "GitHub repo management — security, workflows, releases, health checks. Automates repo hardening, Dependabot, CodeQL, branch protection, releases, and PR management. Use when: (1) Checking or fixing repo security posture, (2) Creating releases or tags, (3) Managing workflows/CI, (4) Repo health audit, (5) Any GitHub operation beyond basic git."
---

# GitHub Operations Skill

You are an expert GitHub repository manager. When invoked, you handle ALL GitHub operations autonomously using `gh` CLI via SSH to W2.

## CRITICAL: Auth Scope Awareness
Before ANY operation, check if you have the required scope. Common scope issues:
- `workflow` scope: needed to create/push `.github/workflows/` files
- `admin:repo_hook` scope: needed for some security API calls
- If scope missing: `ssh w2 'gh auth refresh -h github.com -s <scope>'` (requires user browser auth)

## Core Capabilities

### 1. Security Hardening (run on ANY new or existing repo)
Check and enable ALL security features automatically:
```bash
# Check current state
gh api repos/OWNER/REPO --jq '.security_and_analysis'
gh api repos/OWNER/REPO/vulnerability-alerts 2>&1

# Enable everything
gh api -X PUT repos/OWNER/REPO/vulnerability-alerts
gh api -X PATCH repos/OWNER/REPO \
  -f "security_and_analysis[dependabot_security_updates][status]=enabled" \
  -f "security_and_analysis[secret_scanning][status]=enabled" \
  -f "security_and_analysis[secret_scanning_push_protection][status]=enabled"
```

Required files to create:
- `.github/dependabot.yml` — auto-update dependencies (detect ecosystems from repo)
- `.github/workflows/codeql.yml` — code scanning (detect languages from repo)

### 2. Security Alerts Review
```bash
# Dependabot alerts
gh api repos/OWNER/REPO/dependabot/alerts --jq '.[] | {number, package: .security_vulnerability.package.name, severity: .security_advisory.severity, state: .state}'

# Code scanning alerts
gh api repos/OWNER/REPO/code-scanning/alerts --jq '.[] | {number, rule: .rule.id, severity: .rule.security_severity_level, path: .most_recent_instance.location.path}'

# Secret scanning alerts
gh api repos/OWNER/REPO/secret-scanning/alerts --jq '.[] | {number, secret_type: .secret_type, state: .state}'
```

### 3. Repo Health Audit
Check and fix:
- [ ] README exists with badges
- [ ] LICENSE file exists
- [ ] .gitignore appropriate for languages
- [ ] Branch protection on main
- [ ] Security features enabled (see #1)
- [ ] CI/CD workflows present
- [ ] CODEOWNERS if team repo
- [ ] Description and topics set

```bash
# Set description and topics
gh repo edit OWNER/REPO --description "description" --add-topic topic1 --add-topic topic2

# Branch protection
gh api -X PUT repos/OWNER/REPO/branches/main/protection \
  --input - << 'JSON'
{
  "required_status_checks": {"strict": true, "contexts": []},
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
JSON
```

### 4. Release Management
```bash
# Create tag + release with auto-generated notes
gh release create v0.2.0 --title "ARIS v0.2.0" --generate-notes

# Create release with custom notes
gh release create v0.2.0 --title "ARIS v0.2.0" --notes "$(cat CHANGELOG.md | head -50)"

# Upload assets to release
gh release upload v0.2.0 dist/*.tar.gz
```

### 5. Workflow Management
```bash
# List workflows and runs
gh run list --limit 10
gh run view RUN_ID

# Re-run failed workflow
gh run rerun RUN_ID

# Disable/enable workflow
gh workflow disable WORKFLOW_NAME
gh workflow enable WORKFLOW_NAME
```

### 6. PR Management
```bash
# List open PRs (including Dependabot)
gh pr list

# Auto-merge Dependabot PRs (patch updates only)
gh pr list --author "app/dependabot" --json number,title | jq '.[].number' | while read n; do
  gh pr review $n --approve
  gh pr merge $n --auto --squash
done

# Review PR
gh pr view NUMBER
gh pr diff NUMBER
gh pr checks NUMBER
```

### 7. Badge Generation
Standard badges for ARIS repos:
```markdown
[![npm](https://img.shields.io/npm/v/PACKAGE?style=for-the-badge)](https://www.npmjs.com/package/PACKAGE)
[![MCP](https://img.shields.io/badge/MCP-compatible-6366f1?style=for-the-badge)](https://modelcontextprotocol.io)
[![Skills](https://img.shields.io/badge/skills-COUNT+-34d399?style=for-the-badge)](#skills)
[![Tools](https://img.shields.io/badge/MCP%20tools-COUNT-22d3ee?style=for-the-badge)](#core-systems)
[![CodeQL](https://github.com/OWNER/REPO/actions/workflows/codeql.yml/badge.svg)](https://github.com/OWNER/REPO/actions/workflows/codeql.yml)
```

## Execution Rules
1. ALL GitHub operations via `ssh w2 'gh ...'` — NEVER from Mac
2. Before security changes, always backup current state
3. For workflow pushes, verify `workflow` scope first
4. Auto-detect languages/ecosystems from repo for Dependabot/CodeQL config
5. Report results as table: Feature | Before | After | Status
6. If Dependabot PRs exist, offer to auto-merge patch updates
