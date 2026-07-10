# {PROJECT_NAME} — Execution Plan
# Build order, dependencies, and module sequence

## Build Waves
Modules with no inter-dependencies build in parallel.

### Wave 1 (Foundation)
| Module | Estimated Depth | Dependencies |
|---|---|---|
| {module} | {sessions needed} | None |

### Wave 2
| Module | Estimated Depth | Dependencies |
|---|---|---|
| {module} | {sessions needed} | {Wave 1 modules} |

### Wave 3 (Integration)
| Module | Estimated Depth | Dependencies |
|---|---|---|
| {module} | {sessions needed} | {Wave 1+2 modules} |

## Integration Checkpoints
After each wave:
- [ ] All module tests pass
- [ ] Cross-module contracts validated
- [ ] No regressions in previous waves

## Final Delivery
- [ ] E2E tests pass
- [ ] Security scan (semgrep 0 findings)
- [ ] Dialectic review (0 findings)
- [ ] Browser/device verified
- [ ] Deploy to staging
- [ ] el usuario approval
