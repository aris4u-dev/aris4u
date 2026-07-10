# RUN — aris4u
> Cómo lanzar para test desde el Mac. Generado 2026-07-02 desde configs reales.

## Componentes lanzables

### 1. Live Console (servidor local)
```bash
cd ${ARIS4U_ROOT}/console
${ARIS4U_ROOT}/.venv312/bin/python3 -m aris4u_console.server
```
- **URL:** `http://127.0.0.1:8787` — bind ESTRICTO a 127.0.0.1 (jamás 0.0.0.0)
- **Flags opcionales:** `--port 8787` (default) · `--repo PATH` · `--no-open`
- **Fuente:** `console/aris4u_console/server.py:43,956`

#### Selfcheck (contra server vivo en :8787)
```bash
cd ${ARIS4U_ROOT}/console
${ARIS4U_ROOT}/.venv312/bin/python3 -m aris4u_console.selfcheck
# con salida JSON (CI / la propia consola):
${ARIS4U_ROOT}/.venv312/bin/python3 -m aris4u_console.selfcheck --json
```
- **Fuente:** `console/README.md:11-12` · `console/aris4u_console/selfcheck.py:11`

### 2. MCP Server (stdio — lo lanza Claude Code, no el usuario directamente)
```bash
# Lanzamiento manual de referencia:
bash ${ARIS4U_ROOT}/integrations/mcp_wrapper.sh
# Equivalente directo:
${ARIS4U_ROOT}/.venv312/bin/python3 \
  ${ARIS4U_ROOT}/integrations/mcp_server.py
```
- **Protocolo:** stdio (FastMCP) — no expone puerto TCP
- **Tools:** `aris_search` · `aris_ingest` · `aris_recall_client` · `aris_dialectic` · `aris_health` · `aris_structure` · `aris_critique`
- **Fuente:** `integrations/mcp_wrapper.sh` · `integrations/mcp_server.py:1-15`

### 3. Generadores estáticos (sin servidor)
```bash
cd ${ARIS4U_ROOT}/console
.venv312/bin/python3 -m aris4u_console.inventory      # → out/inventory.json
.venv312/bin/python3 -m aris4u_console.render_console # → out/console.html
```
- **Fuente:** `console/README.md:40-41`

## Dependencias previas
```bash
cd ${ARIS4U_ROOT}
bash install.sh          # idempotente: crea .venv312, pip install -e ., smoke-test gate
# Requisitos: Python ≥ 3.11 · jq · Ollama (opcional — degrada limpio si falta)
```
- **Fuente:** `install.sh:1-50` · `README.md` (sección Requisitos) · `pyproject.toml:8`

## Tests

### Suite principal (engine — rápida, sin integration)
```bash
cd ${ARIS4U_ROOT}
.venv312/bin/python3 -m pytest tests/ -q
# excluye por defecto: -m 'not integration' (Ollama/modelos locales, RAM-heavy)
```
- **Fuente:** `pyproject.toml:[tool.pytest.ini_options]`

### Tests de la Console
```bash
cd ${ARIS4U_ROOT}/console
${ARIS4U_ROOT}/.venv312/bin/python3 -m pytest tests/ -q
```
- **Fuente:** `console/README.md:44`

### Tests de integración (requieren Ollama + modelos cargados)
```bash
cd ${ARIS4U_ROOT}
.venv312/bin/python3 -m pytest tests/ -m integration -q
```
- **Fuente:** `pyproject.toml:markers`

## Respaldo
- **GitHub:** `origin https://github.com/aris4u-dev/aris4u.git` (fetch + push)
- **W2:** espejo rsync en `w2:/media/YOUR_USERNAME/CLAUDE/projects-mirror/` (actualizado 2026-07-02)

> Respaldo actualizado 2026-07-02: GitHub PRIVADO https://github.com/aris4u-dev/aris4u (ya existente) · espejo W2 `w2:/media/YOUR_USERNAME/CLAUDE/projects-mirror/` OK.
