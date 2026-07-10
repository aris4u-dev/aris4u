# ARISWorkflow DAG Guide — Phase B.2

Resumible workflow engine for ARIS4U V16+. Enables checkpointing, resumption, and time-travel debugging of complex multi-step assessments.

## What is ARISWorkflow?

A minimal DAG abstraction over SQLite-backed checkpointing. Build directed acyclic graphs of assessment steps (recon → scan → exploit → report), execute them with automatic state persistence, and resume from checkpoints if failures occur.

**Key Features:**
- Linear and conditional DAGs
- Automatic checkpointing after each node
- Resume from last checkpoint or specific checkpoint by ID
- Time-travel: load checkpoint, modify state, re-execute
- Thread-safe: separate checkpoint streams per thread_id
- No external dependencies: falls back to in-memory state machine

## Quick Start (5 minutes)

```python
from engine.v16.workflow import ARISWorkflow

# 1. Build workflow
wf = ARISWorkflow("assessment")

async def recon_fn(state):
    return {"hosts": ["192.168.1.1"]}

async def scan_fn(state):
    return {"findings": ["CVE-001"]}

wf.add_node("recon", recon_fn)
wf.add_node("scan", scan_fn)
wf.add_edge("recon", "scan")
wf.set_entry("recon")

# 2. Compile
compiled = wf.compile()

# 3. Run
result = compiled.run({"target": "clinic.local"}, thread_id="eng-001")

# 4. If fails mid-way, resume from checkpoint
result = compiled.resume(thread_id="eng-001")
```

## Building DAGs

### Linear Pipeline (A → B → C)

```python
wf = ARISWorkflow("my_workflow")
wf.add_node("a", async_fn_a)
wf.add_node("b", async_fn_b)
wf.add_node("c", async_fn_c)
wf.add_edge("a", "b")
wf.add_edge("b", "c")
wf.set_entry("a")
```

### Conditional Edges (HITL Gate Pattern)

```python
def human_approved(state):
    return state.get("approval_status") == "approved"

wf = ARISWorkflow("assessment")
wf.add_node("scan", scan_fn)
wf.add_node("exploit", exploit_fn)
wf.add_node("cleanup", cleanup_fn)

# If approved, go to exploit; otherwise go to cleanup
wf.add_edge("scan", "exploit", human_approved)
wf.add_edge("scan", "cleanup", lambda s: not human_approved(s))
wf.set_entry("scan")
```

## Checkpoint API

### Save Checkpoints (Automatic)

Checkpoints are saved automatically after each node:

```python
compiled = wf.compile()
result = compiled.run(initial_state, thread_id="eng-001")
# Checkpoints saved at: step_1, step_2, step_3 if all succeed
```

### List Checkpoints

```python
checkpoints = compiled.list_checkpoints("eng-001")
for cp in checkpoints:
    print(cp["node"], cp["timestamp"])
```

### Resume from Latest

```python
result = compiled.resume(thread_id="eng-001")
```

### Resume from Specific Checkpoint

```python
checkpoints = compiled.list_checkpoints("eng-001")
specific_cp_id = checkpoints[1]["checkpoint_id"]
result = compiled.resume(thread_id="eng-001", checkpoint_id=specific_cp_id)
```

### Time-Travel (Debugging)

Load a checkpoint, modify state, and re-execute:

```python
checkpoints = compiled.list_checkpoints("eng-001")
modified = {"hosts": ["10.0.0.1"]}  # Override targets
result = compiled.time_travel(
    thread_id="eng-001-debug",
    checkpoint_id=checkpoints[0]["checkpoint_id"],
    modified_state=modified
)
```

## HITL Gate Integration (Phase B.3)

Placeholder for human-in-the-loop approval gate:

```python
def needs_approval(state):
    critical_count = len([f for f in state.get("findings", []) 
                         if f["severity"] == "critical"])
    return critical_count > 0

wf = ARISWorkflow("assessment_with_hitl")
wf.add_node("scan", scan_fn)
wf.add_node("request_approval", request_hitl_fn)
wf.add_node("exploit", exploit_fn)

wf.add_edge("scan", "request_approval", needs_approval)
wf.add_edge("request_approval", "exploit")
```

In Phase B.3, `request_hitl_fn` will:
1. Create HITL checkpoint in sessions.db
2. Block until human approval (via REST API or CLI)
3. Update state with approval status
4. Continue to exploit node

## Storage

Checkpoints stored in SQLite at `~/.claude-mem/claude-mem.db` (default).

Schema:
```sql
CREATE TABLE workflow_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    node TEXT NOT NULL,
    state TEXT NOT NULL,  -- JSON
    timestamp TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

Override default path:
```python
wf = ARISWorkflow("my_wf", checkpoint_db="/custom/path/db.sqlite")
```

## Testing

```bash
# All workflow tests
pytest tests/test_workflow.py -v

# Resumability tests (critical)
pytest tests/test_workflow_resumability.py -v

# Specific test
pytest tests/test_workflow_resumability.py::TestResumability::test_checkpoint_saved_before_failure -v
```

## Migration from F8 Assessment

**Before (F8 module):**
```python
workflow = AssessmentWorkflow(scope)
session = await workflow.start()
await workflow.run_recon(session)
await workflow.run_scan(session, recon)
# Can't resume if fails here
```

**After (ARISWorkflow):**
```python
from engine.v16.workflow_examples import build_assessment_workflow

wf = build_assessment_workflow()
compiled = wf.compile()
result = compiled.run(initial_state, thread_id="eng-001")
# If fails anywhere, resume:
result = compiled.resume(thread_id="eng-001")
```

## Limitations & Future

- **No automatic retries** (Phase B.3): add `retry_count` parameter to `add_node()`
- **No timeout enforcement** (Phase B.3): add `timeout_seconds` per node
- **No distributed execution** (Phase C): currently sequential only
- **No parallel branches** (Phase C): DAG is acyclic but not parallelizable yet

## Dependencies

- Python 3.11+
- sqlite3 (stdlib)
- No external ML/AI libraries required

## References

- Architecture: `architecture/V16.3_ARCHITECTURE.md`
- F8 Assessment: `engine/v16/f8_assessment.py`
- Examples: `engine/v16/workflow_examples.py`
- Tests: `tests/test_workflow.py`, `tests/test_workflow_resumability.py`
