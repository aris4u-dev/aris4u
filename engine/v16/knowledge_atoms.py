"""V16 Knowledge Atoms — condensed research for hook injection.

Each atom is 2-5 lines of actionable knowledge injected by hooks
at the RIGHT moment based on query classification.

25 atoms across 6 categories (research=4, implementation=7, decision=5,
agent_dispatch=4, fix=2, planning=3). Distilled from 16 V16 research docs
(35K+ lines). See M13_knowledge_atoms/README.md for semantic decomposition.

DO NOT hardcode specific stats (file sizes, row counts) inside atom content
— use structural descriptions. Stats go stale silently and become epistemic
corruption when injected to Claude. For live counts, see
architecture/V16.3_ARCHITECTURE.md §16 Re-verification commands.
"""

ATOMS: dict[str, list[dict]] = {
    "research": [
        {
            "id": "R01",
            "content": "Use 10+ SPECIFIC search queries, not generic. Verify EVERY source URL. Search for COUNTEREXAMPLES to your first answer. Loop: generate → validate → deepen → re-validate until convergence.",
            "source": "Session 0423b methodology",
        },
        {
            "id": "R02",
            "content": "Before claiming 'no prior art exists', search 5+ variations of the query. STU-PID was missed because we searched 'PID LLM' but not 'token budget control feedback'. The information IS finite and findable.",
            "source": "DEEP_F2_TOKEN_BUDGET.md — STU-PID correction",
        },
        {
            "id": "R03",
            "content": "MTEB benchmarks != your specific task. bge-m3 scores higher on MTEB cross-lingual but performed WORSE on our exemplar set (5/10 vs 7/10). Always test empirically on YOUR data, not benchmarks.",
            "source": "DEEP_F1 — bge-m3 revert",
        },
        {
            "id": "R04",
            "content": "Foundational depth scale 1-100: Level 1=axioms, 40=information theory, 70=modern ML, 100=implementation. If you cite an axiom, DERIVE from it — don't just name-drop. Honest depth assessment: we typically start at ~38.",
            "source": "FOUNDATIONAL_DEPTH_AUDIT.md",
        },
    ],
    "implementation": [
        {
            "id": "I01",
            "content": "Mac M5 Pro: 18 cores, 48GB, NO CUDA. W2: 16 cores, 30GB, RTX 3070 8GB CUDA. Training → W2. Inference → Mac (Ollama). Max 2-3 models concurrent on Mac. Max ~13B quantized on W2 GPU.",
            "source": "HARDWARE_AUDIT_VERIFIED.md",
        },
        {
            "id": "I02",
            "content": "Ollama embedding (mxbai-embed-large): 38ms warm, 4s cold (model loading). ALWAYS pre-warm models. Cache embeddings to disk (npz) to avoid recomputation. Hook budget: <200ms total.",
            "source": "F1 disk cache implementation",
        },
        {
            "id": "I03",
            "content": "Anthropic token counting API: POST /v1/messages/count_tokens. FREE. <10ms. 99% accurate. REPLACES chars/4 estimation. Use anthropic SDK.",
            "source": "DEEP_F1/F6 research",
        },
        {
            "id": "I04",
            "content": "Code validation = RUN TESTS, not semantic analysis. range(n) vs range(n+1) look identical in text embeddings but are functionally different. Semantic entropy only for prose/decisions.",
            "source": "DEEP_F5_SEMANTIC_ENTROPY.md — ConSelf correction",
        },
        {
            "id": "I05",
            "content": "LLM-as-judge True Negative Rate < 25%. Judges say 'yes' too often. Use minority-veto: if ANY judge says FAIL, investigate. 3-model ensemble reduces bias 30-40%.",
            "source": "DEEP_F5_SEMANTIC_ENTROPY.md",
        },
        {
            "id": "I06",
            "content": "Saga compensation UNNECESSARY for LLM agents — can't unsend emails or unpublish articles. Simple retry + circuit breaker (3 failures → open → 30s timeout → half-open) sufficient for 99% of cases.",
            "source": "DEEP_F4_AGENT_ORCHESTRATION.md",
        },
        {
            "id": "I07",
            "content": "CPM (Critical Path Method) scheduling is UNIQUE to ARIS — LangGraph, CrewAI, Temporal don't have it. This is a real competitive advantage. Keep and strengthen.",
            "source": "DEEP_F4 competitive analysis",
        },
    ],
    "decision": [
        {
            "id": "D01",
            "content": "ARIS4U = Claude's enhancement layer (MYTHOS local). Claude IS the brain. ARIS4U IS the disk. Three functions: DEPTH (force 10-level analysis), MEMORY (connect sessions), BRIDGE (route to local models). NOT an alternative brain.",
            "source": "Architecture LOCKED — session 0419a (V12 origin, still load-bearing in V16.3)",
        },
        {
            "id": "D02",
            "content": "Rule #0: Claude skips voluntary tools 100%. ONLY hooks work. If you want enforcement, it MUST be a hook. MCP tools are for hooks to call internally, not for Claude to call voluntarily.",
            "source": "Rule #0 — session 0421a",
        },
        {
            "id": "D03",
            "content": "Two-phase workflow: Phase 1 (Architecture — interactive with el usuario, produces .planning/). Phase 2 (Execution — autonomous, reads .planning/ as instructions). NEVER skip Phase 1.",
            "source": "Workflow Model LOCKED (V12 origin, still load-bearing in V16.3)",
        },
        {
            "id": "D04",
            "content": "Memory (V18 Fase E, desacoplado): sessions.db es la fuente ÚNICA — decisions/guards/digests + observations_local (texto cross-session propio, FTS5, queryable via `aris_search`/`aris_recall_client`). aris_vectors.db = sidecar vectorial sqlite-vec (recall semántico). MEMORY.md = índice ejecutivo auto-cargado. claude-mem.db (plugin 3er-party) RETIRADO. Live counts: `sqlite3 data/sessions.db 'SELECT COUNT(*) FROM observations_local'` + `... FROM digests`.",
            "source": "V16 Loop 12-13; desacople claude-mem V18 Fase E (2026-07-02)",
        },
        {
            "id": "D05",
            "content": "GSD has 81 skills including /gsd-autonomous. Don't rebuild workflows — enhance GSD. Expert pattern = SKILL.md files + hook routing + subagent isolation, not '300 expert personas'.",
            "source": "V16 Loop 14 + EXPERT_PATTERN_RESEARCH.md",
        },
    ],
    "agent_dispatch": [
        {
            "id": "A01",
            "content": "Circuit breaker per agent: 3 failures → OPEN (stop calling). 30s timeout → HALF-OPEN (test one call). Success → CLOSED. Prevents cascading failures in multi-agent dispatch.",
            "source": "PSEUDOCODE_AND_CLEANUP.md — circuit breaker",
        },
        {
            "id": "A02",
            "content": "DAG waves via topological sort + antichains (Dilworth theorem). Agents in same wave have NO dependencies → run parallel. Critical path = longest chain → determines minimum duration.",
            "source": "ARIS4U_MASTER.md — agent dispatch DAG design",
        },
        {
            "id": "A03",
            "content": "Mac: max 4 heavy agents parallel (18 cores, memory pressure). W2: offload GPU-bound work. Don't dispatch 10 agents on Mac — will starve. Each Agent() = ~2-4GB memory impact.",
            "source": "HARDWARE_AUDIT_VERIFIED.md",
        },
        {
            "id": "A04",
            "content": "Agent prompts MUST include: domain context (what the product IS), specific requirements (from spec), quality criteria (what DONE looks like), guards (constraints). Generic prompts produce garbage.",
            "source": "CLAUDE.md Agent Prompt Protocol",
        },
    ],
    "fix": [
        {
            "id": "X01",
            "content": "Reproduce FIRST, fix SECOND. Verify the bug exists before writing code. If you can't reproduce it, you can't verify the fix. Test with the EXACT input that triggered the failure.",
            "source": "Depth protocol — fix workflow",
        },
        {
            "id": "X02",
            "content": "Security tasks → route to W2 models (xploiter, Foundation-Sec). Pentesting → qwen35-pentester (temp=0.3). Analysis → qwen35-analyst (temp=0.4). Code gen → qwen2.5-coder (temp=0.15).",
            "source": "config.py model routing",
        },
    ],
    "planning": [
        {
            "id": "P01",
            "content": "Gap coverage matrix: map EVERY requirement to a CONCRETE solution (algorithm + tool + effort). COVERED = has pseudocode. PARTIAL = mentioned but vague. UNCOVERED = not addressed. Target: 0 UNCOVERED.",
            "source": "V16 Loop 2 — gap coverage methodology",
        },
        {
            "id": "P02",
            "content": "Contract audit: verify F_n OUTPUT matches F_{n+1} INPUT at every boundary. Check feedback loops. Identify shared components (SQLite, embeddings). One mismatch breaks the pipeline.",
            "source": "V16 Loop 2 — contract audit methodology",
        },
        {
            "id": "P03",
            "content": "Stack feasibility: map every solution to ACTUAL hardware (Mac M5 48GB no CUDA, W2 RTX 3070 8GB). FEASIBLE = exists, runs, <500 LOC. NOT_FEASIBLE = needs GPU we don't have or >2000 LOC.",
            "source": "V16 Loop 2 — stack feasibility methodology",
        },
    ],
}
