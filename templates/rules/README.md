# templates/rules/ — Capa Constitucional ARIS4U (versiones distribuibles)

## Qué es esto

Versiones **genéricas/plantilla** de las 4 rules clave de ARIS4U (`hardware.md`, `model-governance.md`,
`parallel-dispatch.md`, `RULES.md`). Contienen la lógica constitucional del sistema pero **sin datos
personales** del autor original — hardware específico, clientes, rutas y modelos son **placeholders `{{...}}`**
que cada instalación nueva debe completar.

Propósito: que un usuario externo obtenga la Capa 2 constitucional de ARIS4U como punto de partida
rellenable, en lugar de recibir datos privados del mantenedor original.

---

## Cómo instanciar

### Opción A — automática (recomendada)

```bash
/aris-init      # detecta hardware, escanea repos, genera ~/.aris4u/config.json
                # luego el skill rellena los placeholders con los valores detectados
```

### Opción B — manual

1. Copia los archivos de este directorio a `~/.claude/rules/`:
   ```bash
   cp templates/rules/*.md ~/.claude/rules/
   ```

2. Edita cada archivo y reemplaza los `{{PLACEHOLDER}}` con tus valores reales.

3. Registra las rules en `~/.claude/settings.json` bajo `rules:` (o el mecanismo de carga de tu instalación de Claude Code).

---

## Tabla de placeholders

### hardware.md

| Placeholder | Descripción | Ejemplo |
|-------------|-------------|---------|
| `{{ORCHESTRATOR_HOST}}` | Nombre del equipo orquestador | `M5`, `MacBook Pro`, `Workstation` |
| `{{ORCHESTRATOR_CHIP}}` | Chip del orquestador | `Apple M5 Pro`, `AMD Ryzen 9 7950X` |
| `{{CPU_CORES}}` | Número total de CPU cores | `18`, `32` |
| `{{PERF_CORES}}` | Performance cores (Apple Silicon) | `12` |
| `{{EFF_CORES}}` | Efficiency cores (Apple Silicon) | `6` |
| `{{GPU_CORES}}` | GPU cores del orquestador | `20` |
| `{{GPU_BACKEND}}` | Backend de GPU | `MPS`, `CUDA`, `CPU-only` |
| `{{RAM_GB}}` | RAM total en GB | `48`, `64` |
| `{{RAM_SAFE_GB}}` | RAM segura para uso (sin swap) | `40` |
| `{{RAM_MARGIN_GB}}` | Margen de RAM libre | `8` |
| `{{GPU_SAFE_GB}}` | GB seguros en la GPU/Metal pool | `36` |
| `{{DISK_SIZE}}` | Tamaño total del disco | `2 TB` |
| `{{DISK_FREE}}` | Espacio libre en disco | `1.4 TB` |
| `{{OS_VERSION}}` | Versión del OS | `macOS 26.5.1`, `Ubuntu 24.04` |
| `{{LOCAL_MODELS_LIST}}` | Modelos Ollama instalados localmente | `bge-m3, qwen3:35b, gemma4` |
| `{{WORKER_HOST}}` | Hostname del worker remoto | `w2`, `gpu-server` |
| `{{WORKER_TAILSCALE_IP}}` | IP de Tailscale del worker (si aplica) | `YOUR_W2_TAILSCALE_IP` |
| `{{WORKER_CPU_MODEL}}` | CPU del worker | `AMD Ryzen 9 5900HX` |
| `{{WORKER_CPU_CORES}}` | CPU cores del worker | `16` |
| `{{WORKER_RAM_GB}}` | RAM total del worker | `32` |
| `{{WORKER_GPU_MODEL}}` | GPU del worker | `NVIDIA RTX 3070 Laptop` |
| `{{WORKER_GPU_VRAM}}` | VRAM del worker en GB | `8` |
| `{{WORKER_DISK_SIZE}}` | Disco total del worker | `460 GB` |
| `{{WORKER_DISK_FREE}}` | Disco libre del worker | `194 GB` |
| `{{WORKER_OS}}` | OS del worker | `Pop!_OS 24.04` |
| `{{WORKER_MODELS_LIST}}` | Modelos Ollama en el worker | `bge-m3, qwen3:8b, Foundation-Sec-8B` |
| `{{WORKER_SERVICES}}` | Servicios siempre corriendo en el worker | `supabase stack, n8n, stirling-pdf` |
| `{{DEAD_HOSTS}}` | Hosts obsoletos (no despachar) | `W1, W3, W4` |

### model-governance.md

| Placeholder | Descripción | Ejemplo |
|-------------|-------------|---------|
| `{{SESSION_MODEL}}` | Modelo del hilo de sesión | `claude-opus-4-8`, `claude-sonnet-5` |
| `{{MAX_EXPENSIVE_SESSIONS}}` | Sesiones del modelo más caro permitidas simultáneamente | `1`, `2` |
| `{{CONTEXT_THRESHOLD_K}}` | Umbral de contexto (k tokens) para delegar volumen a subagentes | `100` |
| `{{GATE_MODEL}}` | Modelo para gates irreversibles (H4) | `fable`, `claude-opus-4-8` |
| `{{BEST_MODEL}}` | El modelo más capaz/caro de tu roster | `claude-fable-5`, `claude-opus-4-8` |
| `{{REASONING_MODEL}}` | Modelo para síntesis/veredicto | `claude-opus-4-8` |
| `{{WORKER_MODEL}}` | Modelo default para subagentes de trabajo | `claude-sonnet-5` |
| `{{CHEAP_MODEL}}` | Modelo para tareas triviales | `claude-haiku-4-5` |
| `{{BEST_MODEL_INPUT}}` | Precio input del mejor modelo ($/MTok) | `10.00` |
| `{{BEST_MODEL_OUTPUT}}` | Precio output del mejor modelo ($/MTok) | `50.00` |
| `{{REASONING_MODEL_INPUT}}` | Precio input del reasoning model | `5.00` |
| `{{REASONING_MODEL_OUTPUT}}` | Precio output del reasoning model | `25.00` |
| `{{WORKER_MODEL_INPUT}}` | Precio input del worker model | `2.00` |
| `{{WORKER_MODEL_OUTPUT}}` | Precio output del worker model | `10.00` |
| `{{CHEAP_MODEL_INPUT}}` | Precio input del cheap model | `0.25` |
| `{{CHEAP_MODEL_OUTPUT}}` | Precio output del cheap model | `1.25` |

### parallel-dispatch.md

| Placeholder | Descripción | Fuente |
|-------------|-------------|--------|
| `{{RAM_GB}}` | RAM total | hardware.md |
| `{{RAM_SAFE_GB}}` | RAM segura | hardware.md |
| `{{RAM_SATURATE_GB}}` | RAM en la que empieza el swap | hardware.md (medir: ~96% del total) |
| `{{GPU_SAFE_GB}}` | GB seguros GPU | hardware.md |
| `{{GPU_BACKEND}}` | Backend GPU | hardware.md |
| `{{CPU_CORES}}` | CPU cores | hardware.md |
| `{{MAX_CLOUD_AGENTS}}` | = min(16, CPU_CORES-2) | calcular |
| `{{WORKER_HOST}}` | Worker remoto | hardware.md |
| `{{DEAD_HOSTS}}` | Hosts muertos | hardware.md |
| `{{REASONING_MODEL}}` | Modelo reasoning | model-governance.md |
| `{{WORKER_MODEL}}` | Modelo worker | model-governance.md |
| `{{CHEAP_MODEL}}` | Modelo barato | model-governance.md |
| `{{BEST_MODEL}}` | Modelo gate | model-governance.md |

### RULES.md

| Placeholder | Descripción | Ejemplo |
|-------------|-------------|---------|
| `{{ARIS4U_DIR}}` | Ruta absoluta al directorio ARIS4U | `~/projects/aris4u` |
| `{{LAB_PROJECTS}}` | Proyectos de laboratorio (no entregables) | `Lab-Project-1, Lab-Project-2` |
| `{{CLIENT_LIST}}` | Proyectos de clientes con revenue | `Client-B, Client-C, Client-D` |
| `{{DEAD_WORKERS}}` | Workers remotos obsoletos | `W1, W3, W4` |

---

## Qué NO está en estos templates (privado por diseño)

Las siguientes cosas viven en la instalación del usuario, NO en el template distribuible:
- Credenciales de clientes (EIN, API keys, URLs de proyectos específicos)
- Rutas absolutas personales del usuario (se usan `{{ARIS4U_DIR}}` etc.)
- Nombres reales de clientes (se usan `{{CLIENT_LIST}}`)
- Hardware exacto del orquestador (se usan placeholders a rellenar)
- Modelos y precios actuales (verificar siempre en `platform.claude.com` — cambian)

---

## Versionado

Estos templates se sincronizan con la versión de ARIS4U registrada en `.claude-plugin/plugin.json`.
Al actualizar ARIS4U, revisa si hay cambios de semántica en las rules y propaga al template.
