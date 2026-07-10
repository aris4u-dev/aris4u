# ARIS4U — Project Directives
# Plugin v18.0.0 · Updated 2026-07-03
# FUENTE DE VERDAD ÚNICA: architecture/ARIS4U_MASTER.md

## QUÉ ES

Amplificador de capacidad del usuario sobre **Claude Opus 4.8**: memoria cross-session,
guards (estándares del usuario), orquestación multi-modelo, ejecución PHI-safe/auditable.
Cognición = **rentada de Claude**; el plugin potencia el canal, no la cognición.
Revenue clients (Client-B/Client-C/Client-D) = partially deliverable.
Lab-Project-1/Lab-Project-2 = lab (do not burn hours on cosmetic redesigns; capture data).

## STACK

- Python · venv `.venv312/` (`${ARIS4U_ROOT}/.venv312/bin/python3`)
- FastMCP (stdio) · sqlite-vec · pytest-asyncio
- Ollama Mac (hermes3:70b, qwen35-analyst, qwen35-pentester, gemma4-abliterated, mxbai-embed-large)
- Ollama W2 (xploiter, Foundation-Sec-8B, qwen3:8b, bge-m3) — verificar RAM antes de despachar

## MAPA DE MÓDULOS (verificado 2026-07-03)

| Dir | Contenido | Entrada principal |
|-----|-----------|-------------------|
| `engine/v16/` | Pipeline F1-F9: f1_classifier, f2_cognicion, f3_memoria, f5_validacion, f6_comunicacion, f7_aprendizaje, f8_assessment + orchestrator, model_router, locked_scorer, vector_store, soft_reward | `v16_orchestrator.py` |
| `hooks/` | Dispatcher único Python + scripts de soporte | `hooks/dispatch.py` |
| `hooks/dispatch/events/` | 8 event handlers: pre_tool_use, post_tool_use, session_start/end, stop, subagent_start, user_prompt_submit, _briefing | por evento |
| `hooks/dispatch/handlers/` | ~15 sub-handlers: migration_linter, phi_guard (BLOCK exit 2), code_quality_gate, parallel_dispatch_guard, schema_drift, mcp_guard, phi_sanitizer, redact, etc. | encadenados por dispatch.py |
| `integrations/` | MCP server (7 tools vía FastMCP) | `mcp_server.py` |
| `console/aris4u_console/` | Live Console: server, selfcheck, render_console, inventory, capabilities. **Endpoints cowork:** `GET /project?client=` (timeline JSON) · `GET /project/stream?client=` (SSE en vivo) · `GET /project/comments?client=&sha=` · `POST /project/comment` (guard CSRF same-origin). | `server.py` · `selfcheck.py` |
| `tools/` | CLI tools: aris_config, aris_status, capability_router, capability_inventory, model_router, recall_usefulness, contract_gate, **project_timeline** (read-model cowork: une git log + sessions.db por commit_sha → timeline JSON por cliente), etc. | top-level scripts |
| `data/` | DB de sesiones/decisions/guards + sidecar vectorial + embeddings | `sessions.db` · `aris_vectors.db` |
| `architecture/` | Docs canónicos vivos | `ARIS4U_MASTER.md` · `V18_BLUEPRINT.md` · `LEDGER_HISTORICO_ARIS4U.md` |
| `templates/planning/` | Plantillas de planning (ARCHITECTURE, EXECUTION, QUALITY, MODULE_SPEC) | — |
| `.claude-plugin/` | Manifest del plugin | `plugin.json` (v18.0.0) |
| `agents/` · `skills/` | Agents y skills del plugin (auto-descubiertos) | — |
| `tests/` | Suite pytest (dispatch/, engine/, integration/) | `pytest tests/` |

## COMANDOS CANÓNICOS

```bash
# Tests
cd ${ARIS4U_ROOT}
.venv312/bin/python3 -m pytest tests/ -x -q

# Live Console (bind estricto 127.0.0.1:8787)
cd ${ARIS4U_ROOT}/console
.venv312/bin/python3 -m aris4u_console.server

# Selfcheck (requiere server corriendo en :8787)
cd ${ARIS4U_ROOT}/console
.venv312/bin/python3 -m aris4u_console.selfcheck
.venv312/bin/python3 -m aris4u_console.selfcheck --json   # para CI

# MCP server (referencia — Claude Code lo lanza vía mcp_wrapper.sh)
bash ${ARIS4U_ROOT}/integrations/mcp_wrapper.sh
```

## MCP TOOLS (7 — verificados: 7× @mcp.tool() en integrations/mcp_server.py)

| Tool | Cuándo |
|------|--------|
| `aris_search` | Buscar decisions/guards/digests por texto o semántica; scope `client` opcional |
| `aris_ingest` | Guardar una decisión o guard para recall futuro; tag `client` opcional |
| `aris_recall_client` | Recall decisions/guards scoped a un cliente (Client-B/Client-C/Client-D) |
| `aris_dialectic` | Review Builder+Reviewer+Security de código crítico (Ollama local) |
| `aris_structure` | F1 PRE-amplificación: estructura idea cruda → spec (opt-in) |
| `aris_critique` | F1 POST-amplificación: critica multi-ángulo → FLAGS (opt-in) |
| `aris_health` | Estado del sistema: Ollama Mac+W2 vivos, sessions.db stats |

NO existen: `aris_recall` / `aris_ask` / `aris_dispatch` (eliminados en V16.9).

## HOOKS — DISPATCHER ÚNICO

`~/.claude/settings.json` + `hooks/hooks.json` cablean 7 eventos a `hooks/dispatch.py`.
PreToolUse encadena ~11 sub-handlers; PostToolUse ~5.
Bloqueantes reales (exit 2): **migration_linter** y **phi_guard**.
Scripts vivos (no portados): `write_client_bridge.sh`, `async_vacuum.sh`, `nightly_vacuum.sh`.
Para agregar handlers: editar `hooks/dispatch/handlers/`; NO crear `.sh` nuevos de raíz.

## DIRECTIVAS OPERACIONALES

- **Antes de tocar código**: `source ~/projects/aris4u/tools/start_validation_session.sh`;
  cada bug/fix = entry JSONL + root-cause en VALIDATION_LOG.
- **Lab (Lab-Project-1/Lab-Project-2)**: fix bugs con evidencia; NO rediseños cosméticos;
  NO empezar un build sin directorio `.planning/` con spec aprobada.
- **Decisiones locked** (`sessions.db locked=1`): NO contradecir sin evidencia nueva primero.
- **Fable Gate**: antes de acción grande/irreversible (versión, migración, borrado, rumbo),
  pasar por `Agent(model="fable")` → veredicto → ejecutar.
- **Memoria**: `data/sessions.db` = fuente única (decisions/guards/digests + observations_local FTS5);
  `data/aris_vectors.db` = sidecar vectorial. `claude-mem.db` = RETIRADO.
- **Cache TTL**: ENABLE_PROMPT_CACHING_1H=true → 1h TTL asumido. Verificar mensualmente
  si hooks invocan Claude API directo (SDK regressed 1h→5m en marzo 2026).

## NO TOCAR / NO REFERENCIAR

- Paths muertos: `architecture/V16.9_ARCHITECTURE.md`, `architecture/INDEX.md`,
  `architecture/audits/AUDIT_V2_20260611.md`, `.planning/audit-0423f/`
- Retirados: `brain.db` · `claude-mem.db` (V18) · workers W1/W3/W4 · módulos design_parser,
  gate, voting, iteration_guard, F3_MEMORIA_QUICKREF, 27 bio-inspired engines
- "V17" no existe como release (el motor salta 16.10→18.0.0) · `aris_integrity_check` en DB >1GB
