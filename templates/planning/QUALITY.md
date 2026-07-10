# {PROJECT_NAME} — Quality Gates

## Per Module
- [ ] Code complete (all requirements from spec)
- [ ] Unit tests ≥90% coverage
- [ ] Integration tests pass
- [ ] Security scan: semgrep 0 findings
- [ ] Dialectic review: Builder + Reviewer + Security → 0 findings
- [ ] Browser verified (for UI modules)
- [ ] Contract valid (matches contracts/)

## Per Wave
- [ ] All modules in wave pass individual gates
- [ ] Cross-module integration tests pass
- [ ] No regressions in prior waves

## Project-Wide
- [ ] Architecture approved by el usuario (Phase 1 complete)
- [ ] All waves complete
- [ ] E2E tests: all user flows work end-to-end
- [ ] Performance: page load <2s, API response <500ms
- [ ] Accessibility: semantic HTML, aria-labels
- [ ] Deploy: staging verified, production ready
- [ ] el usuario final approval

## Stop Conditions (Phase 2 → interrupt el usuario)
- Scope violation: requirement not in architecture
- Critical failure: build cannot proceed
- Ambiguity: architecture doesn't cover this case
- Security: vulnerability found that changes design
