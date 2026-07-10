# Security Checklist — Apply to Every New Project

## Data Access (IDOR prevention)
- [ ] Authorization at DATA layer (RLS / ownership check), not just route-level auth
- [ ] UUIDs for public-facing IDs (not sequential integers)

## API Boundaries (Deserialization)
- [ ] No native serialization from clients (no Java ObjectInputStream, no Python pickle)
- [ ] JSON/protobuf only with strict schema validation

## HTTP Outbound (SSRF)
- [ ] Any URL-fetch feature: allowlist destinations, block internal IPs
- [ ] DNS resolution before connection (anti-DNS rebinding)

## Dependencies (Supply Chain)
- [ ] Private packages scoped (@company/*)
- [ ] Exact version pinning, lockfiles committed
- [ ] Dependabot or equivalent enabled

## Secrets
- [ ] No secrets in git, env vars, logs, or debug output
- [ ] Secret manager (Vault/Keychain) for all credentials
- [ ] gitleaks pre-commit hook

## Containers (if applicable)
- [ ] No --privileged, drop ALL caps, non-root user
- [ ] Read-only rootfs, seccomp profile

## Architecture
- [ ] Threat model before writing code
- [ ] Template engines sandboxed if rendering user input
- [ ] Constant-time comparison for secrets/tokens
