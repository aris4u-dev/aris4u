# ARIS4U — Guia de instalacion y primeros pasos

> Para usuarios nuevos de Claude Code (cuenta Max) que quieren instalar ARIS4U como plugin.

---

## Que es ARIS4U

ARIS4U es un **plugin de Claude Code** que envuelve cada sesion con tres capas:

- **Memoria local** — tres bases de datos en disco (narrativa FTS5 + decisiones/guards por proyecto + vectores semanticos) que persisten entre conversaciones.
- **Gobernanza** — guards bloqueantes (`PreToolUse`) que previenen errores comunes en tiempo real (type hints, imagenes Docker sin version fija, RLS de Supabase, entre otros).
- **7 herramientas MCP** — disponibles desde cualquier sesion de Claude Code para buscar memoria, revisar codigo con roles locales y mas.

La cognicion la aporta Claude; ARIS4U pone la capa de memoria y reglas alrededor. No llama a ningun modelo externo ni gasta tokens propios.

---

## Requisitos

| Requisito | Tipo | Notas |
|-----------|------|-------|
| **Python >= 3.11** (3.12 recomendado) | Obligatorio | `install.sh` verifica la version; crea el venv en `.venv312/` |
| **Claude Code CLI** | Obligatorio | Cuenta Max; ARIS4U cabalga su event bus |
| **jq** | Obligatorio | Lo usan `hooks/redact_secrets.sh` y `hooks/guards/gpu-crash-guard.sh` |
| **Ollama** (local) | Opcional | Necesario para memoria semantica (`mxbai-embed-large`) y revision multi-rol (`aris_dialectic`); si no esta, ARIS4U degrada limpio: FTS5 y guards siguen activos |

Instala `jq` si no lo tienes:

```bash
# macOS
brew install jq

# Debian / Ubuntu
sudo apt install jq
```

---

## Instalacion

### 1. Clonar el repositorio

```bash
git clone https://github.com/aris4u-dev/aris4u ~/projects/aris4u
cd ~/projects/aris4u
```

### 2. Ejecutar el instalador

```bash
bash install.sh
```

El script `install.sh` hace los siguientes pasos en orden:

1. **Pre-check Python** — verifica `python3 >= 3.11`; falla con mensaje claro si no cumple.
2. **Crea el venv** — `.venv312/` con `python3 -m venv`.
3. **Instala dependencias** — `pip install -e .` (httpx, mcp[cli], numpy, scipy, sqlite-vec).
4. **Detecta Ollama** — avisa si no esta disponible (no aborta).
5. **Gate del contrato** — corre `tools/adapt/smoke_test.py`; falla si el contrato no carga.
6. **`aris4u init`** — genera `~/.aris4u/config.json` (ver seccion siguiente). Si ya existe, lo preserva.

**Flags disponibles de `install.sh`:**

| Flag | Efecto |
|------|--------|
| *(ninguno)* | Interactivo: pide confirmacion de proyectos detectados |
| `--yes` | No-interactivo: auto-detecta hardware y proyectos (util para CI) |
| `--no-init` | Omite `aris4u init`; genera la config despues con `python3 tools/aris4u_init.py` |
| `--cron` | Instala el cron de auto-adaptacion diaria (modo SOMBRA, LaunchAgent en macOS) |

### 3. Registrar el plugin en Claude Code

```bash
claude plugin marketplace add ~/projects/aris4u
claude plugin install aris4u
claude plugin validate ~/projects/aris4u
```

Claude Code auto-descubre `skills/`, `hooks/hooks.json` y `.mcp.json` del directorio del plugin.

> **Nota si ya tenias ARIS4U cableado manualmente en `~/.claude/settings.json`:** los 24 hooks
> corrererian dos veces (settings.json + plugin). Quita los bloques aris4u de `settings.json`
> (con backup) despues de confirmar que el plugin carga. `install.sh` no lo hace por ti.

### 4. Verificar la activacion

Abre una **sesion nueva** de Claude Code (el plugin se activa en la siguiente sesion) y ejecuta:

```
/aris-status
```

Debes ver el estado de hooks, MCP tools, guards activos y contadores de memoria.

---

## Configuracion: `aris4u init`

El instalador corre `python3 tools/aris4u_init.py` para generar `~/.aris4u/config.json`.
Esta config desacopla el hardware y los proyectos de tu maquina del codigo del repo, lo que
hace ARIS4U instalable por terceros sin tocar el codigo fuente.

**Que genera:**
- Deteccion portable de hardware (CPU, RAM, GPU si aplica).
- Escaneo de directorios `~/projects/` buscando repos `.git`.
- Fusion idempotente con config existente: nunca pisa claves del usuario (como `ollama_*` o `w2_*`).
- Permisos `0600` (solo lectura del propietario).

**Flags de `aris4u_init.py`** (si lo corres directamente):

| Flag | Efecto |
|------|--------|
| `--yes` | No-interactivo; incluye todos los proyectos detectados |
| `--dry-run` | Imprime el JSON resultante sin escribir en disco |
| `--force` | Re-inicializa ignorando la config existente |
| `--scan-root <dir>` | Directorio raiz de proyectos (default: `~/projects`) |

La ruta de config es `~/.aris4u/config.json` o la variable `$ARIS4U_CONFIG` si la defines.

Para regenerar despues de un cambio de maquina o de proyectos:

```bash
python3 tools/aris4u_init.py --force
```

---

## Herramientas MCP (7)

Disponibles en cualquier sesion de Claude Code una vez instalado el plugin.
Todas corren localmente; ninguna llama a APIs externas.

| Herramienta | Para que sirve |
|-------------|---------------|
| `aris_ingest` | Guarda una decision o guard en `sessions.db` para que persista en sesiones futuras |
| `aris_search` | Busqueda full-text + semantica sobre digests, decisiones, guards y observaciones |
| `aris_recall_client` | Recupera decisiones y guards bloqueados de un proyecto especifico (memoria por proyecto) |
| `aris_dialectic` | Revision multi-rol en paralelo (Builder + Reviewer + Security) usando modelos Ollama locales |
| `aris_structure` | Pre-amplificacion opt-in: estructura una idea cruda en spec accionable (objetivo/requisitos/riesgos/criterios) |
| `aris_critique` | Post-amplificacion opt-in: critica una respuesta o codigo desde multiples angulos y devuelve banderas concretas |
| `aris_health` | Verificacion de salud del sistema: Ollama local, modelos disponibles y estadisticas de `sessions.db` |

> `aris_dialectic`, `aris_structure` y `aris_critique` requieren Ollama activo con al menos un modelo cargado.
> Si Ollama no responde, cada herramienta lo indica claramente y no bloquea el flujo.

---

## Skills incluidas (4)

Las skills se invocan con `/nombre-de-skill` dentro de cualquier sesion de Claude Code.

| Skill | Descripcion |
|-------|-------------|
| `/aris-init` | Genera o regenera la config por-usuario `~/.aris4u/config.json`; util al cambiar de maquina o agregar proyectos |
| `/aris-client-audit` | Auditoria parametrizada de codigo de un repositorio; revision con roles (Builder + Reviewer + Security) y reporte SARIF con hallazgos por severidad |
| `/aris-council` | Somete una decision o pregunta dificil a 5 lentes de razonamiento independientes (contrarian, first-principles, expansionist, outsider, executor) y sintetiza un veredicto con convergencias y conflictos |
| `/aris-memory-audit` | Audita la memoria de sesion (`sessions.db`) de un proyecto: decisiones bloqueadas, guards y gaps de compliance; detecta contradicciones y datos desactualizados |

---

## Memoria semantica con Ollama (opcional)

Para activar la memoria semantica local instala Ollama y descarga el modelo de embeddings:

```bash
# Instalar Ollama (macOS)
brew install ollama

# Descargar el modelo de embeddings (recomendado)
ollama pull mxbai-embed-large
```

Con Ollama activo, `aris_search` combina busqueda FTS5 (texto exacto) con busqueda vectorial
(similitud semantica) y `aris_dialectic` puede correr la revision multi-rol localmente.

Sin Ollama, los guards y la memoria FTS5 siguen funcionando; solo se desactiva la capa semantica.

---

## Primeros pasos: tu primera sesion

### Lo que ARIS4U hace automaticamente

Desde la primera sesion activa, sin que tengas que invocar nada:

- **Recall automatico** — en cada prompt no trivial, ARIS4U inyecta contexto de sesiones anteriores del proyecto activo.
- **Guards bloqueantes** — antes de cada herramienta (`PreToolUse`), los guards verifican patrones de riesgo comunes y bloquean si detectan un problema (por ejemplo, una imagen Docker con etiqueta `:latest` o una funcion sin anotaciones de tipos).
- **Captura de sesion** — al cerrar, ARIS4U guarda un digest de la sesion en `sessions.db`.

### Capacidades que puedes invocar

```
# Verificar que el plugin esta activo y ver el estado de memoria
/aris-status

# Buscar en la memoria de sesiones anteriores
# (desde Claude, sin slash command: usa la tool MCP directamente)
aris_search("como resolvi X")

# Guardar una decision importante para que persista
aris_ingest("Decision: usar PostgreSQL para la capa de datos", domain="database")

# Revisar un fragmento de codigo con tres roles en paralelo
aris_dialectic("auth/login.py — verificar manejo de tokens JWT")

# Ver la salud del sistema (Ollama, modelos, BD)
aris_health()
```

### Flujo tipico recomendado

1. Abre Claude Code en tu proyecto.
2. ARIS4U inyecta recall automaticamente.
3. Trabaja normalmente; los guards actuan en segundo plano.
4. Cuando tomes una decision importante, usa `aris_ingest` para fijarla.
5. En la siguiente sesion, el recall automatico la trae de vuelta.

---

## Estructura del repositorio (referencia rapida)

| Directorio / Archivo | Contenido |
|---------------------|-----------|
| `install.sh` | Instalador idempotente |
| `tools/aris4u_init.py` | Generador de config por-usuario |
| `integrations/mcp_server.py` | 7 herramientas MCP (FastMCP) |
| `hooks/hooks.json` | Registro de los 24 hooks (7 eventos) |
| `hooks/guards/` | Guards bloqueantes |
| `engine/v16/` | Modulos Python del pipeline F1-F9 |
| `skills/` | 4 skills del plugin |
| `data/sessions.db` | Decisiones y guards por proyecto |
| `architecture/ARIS4U_MASTER.md` | Documento maestro (fuente de verdad unica) |
| `STATUS.md` | Estado vivo del sistema (auto-generado) |

---

## Ayuda y documentacion

- **Estado del sistema**: `/aris-status`
- **Documento maestro**: `architecture/ARIS4U_MASTER.md`
- **Estado vivo**: `STATUS.md` (tiene prioridad sobre cualquier cifra escrita a mano)
- **Arquitectura tecnica**: `architecture/V16.9_ARCHITECTURE.md`
