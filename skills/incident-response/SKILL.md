---
name: incident-response
description: >
  Incident response runbook for cluster emergencies. Standardized procedure for
  service outages, security breaches, data corruption, and performance degradation.
  Includes triage, containment, investigation, resolution, and post-mortem.
  Use when: (1) Service is down, (2) Security breach detected,
  (3) Data corruption found, (4) Performance emergency.
version: 0.1.0
category: security
tags: [incident, response, emergency, outage, security-breach]
---

# Incident Response — Emergency Runbook

## Severity Levels
- **P1 Critical**: Service completely down, security breach, data loss
- **P2 High**: Service degraded, potential breach, backup failure
- **P3 Medium**: Performance issue, non-critical service down
- **P4 Low**: Warning threshold crossed, cosmetic issue

## Response Procedure
1. **TRIAGE** (2 min): Identify scope, severity, affected services
2. **CONTAIN** (5 min): Isolate affected systems, stop bleeding
3. **INVESTIGATE** (15 min): Root cause analysis using logs, metrics, forensics
4. **RESOLVE**: Fix the issue, verify fix works
5. **RECOVER**: Restore from backup if needed, verify data integrity
6. **POST-MORTEM**: Document what happened, why, and prevention measures

## Tools
- security-agent: Security breach investigation
- devops-agent: Container/service recovery (W2 Docker stack)
- `ssh w2 docker ps` / `docker logs <container>`: Estado y logs en W2
- `aris_health` (MCP): Estado agregado Mac + W2 Ollama + Docker containers
- `bash ~/Claude/scripts/deploy-client-c.sh status`: Client-C stack status
- auditd logs (W2): Who did what when

## Cluster (2026-07-03)
- **M5**: MacBook Pro M5 Pro 48GB — orquestador principal, MPS activo
- **W2**: RTX 3070L / 32GB / Pop!_OS — 24/7 services (client-c stack ~12 containers, n8n, umami)
- W1 / W3 / W4: MUERTOS — no referenciar
