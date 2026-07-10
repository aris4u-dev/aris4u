# ARIS4U V18 Dogfooding Sprint Guide

## Overview

A structured 28-day dogfooding sprint to validate ARIS4U V18 design and measure real-world impact. Eight metrics track usability, performance, reliability, and subjective satisfaction.

## Why Dogfooding

Dogfooding (eating your own dog food) ensures ARIS4U works for actual use cases:
- Discover integration gaps before V18 release
- Measure real impact on development workflow
- Identify UI/UX friction points
- Validate token savings claims
- Catch PHI/security issues early

## The 8 Metrics

### 1. session_aris_pct (20% weight)
**What**: Percentage of Claude Code sessions where ARIS4U features are used.

**Target**: 80%

**Source**: claude-mem.db (count observations with ARIS4U tags)

**Interpretation**:
- Below 70%: ARIS4U integration friction or unclear value
- 70-80%: Good adoption, minor friction
- 80%+: Seamless integration, no friction

**If Red**: Review which sessions don't use ARIS4U. Might indicate specific workflows where memory/dispatch/hooks don't help.

---

### 2. recall_hit_rate (15% weight)
**What**: Success rate of claude-mem recall queries in real workflows.

**Target**: 70%

**Source**: claude-mem.db (recall_metrics table)

**Interpretation**:
- Below 60%: Memory state corruption or poor query strategy
- 60-70%: Acceptable, needs optimization
- 70%+: High-quality memory retrieval

**If Red**: Check memory_decay.py logic. May need to rebuild index or improve observation pruning.

---

### 3. hitl_latency_median (15% weight)
**What**: Median time (milliseconds) for HITL gate to approve/deny a decision.

**Target**: 30000ms (30 seconds)

**Source**: dashboard/data/hitl.db

**Interpretation**:
- Below 10s: HITL response time excellent
- 10-30s: Acceptable for human review
- Above 30s: Bottleneck in workflow, consider auto-approval rules

**If Red**: Check if HITL UI is responsive or if manual queue is overloaded. May need additional reviewers.

---

### 4. context_preservation_30d (15% weight)
**What**: Percentage of observations and context surviving 30+ days of cross-session use.

**Target**: 100%

**Source**: claude-mem.db (observation age + decay checks)

**Interpretation**:
- Below 90%: Aggressive memory decay is purging data
- 90-100%: Good retention, minimal loss
- 100%: Perfect retention (ideal)

**If Red**: Review memory_decay.py thresholds. May be pruning too aggressively or decay formula is wrong.

---

### 5. phi_leak_count (20% weight)
**What**: Number of detected PHI leaks in audit log over sprint.

**Target**: 0

**Source**: dashboard/data/audit.jsonl + memory_signer audit logs

**Interpretation**:
- 0: Perfect compliance, no leaks
- 1+: CRITICAL — each leak must be investigated and logged

**If Red (any value > 0)**: 
1. Stop and isolate the incident
2. Check which API/source allowed PHI through
3. Update privacy_router rules
4. Retest before continuing sprint

**Note**: This is the only metric where target=0 is non-negotiable.

---

### 6. lora_adapter_usage (5% weight, informational)
**What**: Distribution of which LoRA adapters are loaded and used vs unused.

**Target**: N/A (informational only)

**Source**: engine/v16/adapter_metrics.db

**Interpretation**:
- Shows which V16 plugins are most/least useful
- Identifies candidates for V17 optimization
- Highlights dead code to remove

**If Notable**: Adapters with <5% usage can be marked for deprecation or consolidation.

---

### 7. api_token_savings (10% weight)
**What**: Percentage reduction in Claude API tokens used vs baseline (no ARIS4U).

**Target**: 40%

**Source**: dashboard/data/token_metrics.db (estimated from logs)

**Interpretation**:
- Below 30%: ARIS4U overhead outweighs memory reuse savings
- 30-40%: Good savings, likely ROI positive
- 40%+: Excellent savings, ARIS4U pays for itself

**If Red**: Check if memory recalls are increasing token counts (long context) vs reducing (fewer API calls). May indicate poor memory quality.

---

### 8. subjective_satisfaction (0% weight, qualitative)
**What**: Weekly score (1-10) reflecting the user's satisfaction with ARIS4U experience.

**Target**: 8/10

**Source**: ~/.aris4u/journal/ (daily/weekly entries)

**Interpretation**:
- 1-4: Major friction, workflow disrupted
- 5-6: Acceptable but frustrating
- 7-8: Good, minor annoyances
- 9-10: Excellent, seamless

**If Below 7**: Qualitative notes in journal entry should identify the pain point. Use this to drive fixes.

---

## Sprint Rules

### Daily
```bash
# Morning: check status
bash scripts/dogfooding_status.sh

# Evening (optional): journal entry
python -m tools.dogfooding.journal entry
```

### Weekly (Every Friday)
1. Review 7-day metric trend
2. Identify red metrics
3. Write weekly summary
   ```bash
   python -m tools.dogfooding.sprint_dashboard report --week N
   ```
4. Journal weekly observations

### Exit Gate (End of 28 Days)
Sprint passes if ALL of:
1. **6 of 8 metrics at or above target** (phi_leak_count must be 0)
2. **4 consecutive weeks of green status**
3. **phi_leak_count = 0** throughout
4. **journal_entries >= 20** (5 per week average)

If any metric stays red >2 weeks, escalate:
- Investigate root cause
- File GitHub issue in ARIS4U
- Adjust usage pattern or ARIS4U configuration
- Re-test before sprint end

## Starting the Sprint

```bash
# 1. Initialize (one-time)
bash scripts/dogfooding_init.sh

# 2. Check status
bash scripts/dogfooding_status.sh

# 3. Create first journal entry
python -m tools.dogfooding.journal entry
```

## During the Sprint

**Treat ARIS4U as your primary tool:**
- Use dispatch for all agent work
- Rely on memory for cross-session context
- Enable all HITL gates
- Monitor dashboard daily

**Track metrics:**
- Dashboard collectors run auto if metrics sources exist
- Manual collection via `python -m tools.dogfooding.sprint_dashboard collect`
- View history: `python -m tools.dogfooding.sprint_dashboard history metric_name`

**Journal regularly:**
- Capture friction, unexpected learnings, feature requests
- Note which adapters you use most
- Record subjective satisfaction (even if informal)

## Output

At end of 28 days:

1. **Metrics report**: `dogfooding_status.sh` final output
2. **Journal summary**: `python -m tools.dogfooding.journal stats`
3. **Weekly reports**: One per week (4 total)
4. **Git log**: Commit activity, dispatch frequency

## Rollover (If Sprint Fails)

If exit gate not met:
1. Identify blocking metrics
2. File P0 GitHub issues for each red metric
3. Create new 28-day sprint
4. Focus sprint on red metric fixes

Example: If recall_hit_rate stays red, next sprint focuses on memory quality with weekly optimization sprints.

## Success Indicators

✓ All 8 metrics dashboard showing (even if values are TBD initially)
✓ `dogfooding_init.sh` creates database and initial sprint row
✓ `scripts/dogfooding_status.sh` displays formatted table
✓ `python -m tools.dogfooding.journal entry` prompts for score + notes
✓ Tests pass: `pytest tests/test_dogfooding_metrics.py -v`

---

**Sprint Duration**: 28 days from initialization
**Review Cadence**: Weekly on Fridays
**Exit Decision**: End of week 4
