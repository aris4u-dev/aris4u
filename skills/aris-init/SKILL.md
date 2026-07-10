---
name: aris-init
description: |
  Genera la config por-usuario de ARIS4U (~/.aris4u/config.json) que hace el sistema
  instalable por terceros. Detecta hardware portable, escanea repos git en ~/projects/,
  y produce un JSON fusionando con config existente (preserva ollama_*/w2_* del usuario).
  Idempotente y no destructivo: corre N veces sobre el mismo directorio da el mismo resultado.
  When to use: (1) primera instalación de ARIS4U, (2) cambio de máquina/usuario,
  (3) actualizar la lista de proyectos escaneados, (4) diagnosticar qué detecta ARIS4U
  sobre el hardware actual. NO toca hooks ni engine/v16/config.py.
  Example: `/aris-init`, `/aris-init --yes`, `/aris-init --dry-run`
---

## When to Use

- **Primera instalación**: Un nuevo usuario acaba de clonar ARIS4U y necesita generar su
  `~/.aris4u/config.json` con hardware, proyectos y URLs de Ollama.
- **Cambio de máquina**: Re-detectar hardware (cores, RAM, chip) y re-escanear proyectos
  en la nueva máquina.
- **Actualizar proyectos**: Añadir repos nuevos a `lab_projects` sin perder configuración.
- **Dry-run de diagnóstico**: Ver qué detectaría init sin escribir nada en disco.
- **Reset limpio**: Reinicializar ignorando la config anterior con `--force`.

## Prerequisites

- Python 3.10+ con el venv de aris4u activado (`.venv312/bin/activate`).
- `psutil` instalado (en el venv) para detección precisa de RAM; fallback a `sysctl`/`/proc/meminfo`.
- Directorio `~/projects/` existente (configurable con `--scan-root`).

## Usage

```bash
/aris-init [--yes] [--dry-run] [--force] [--scan-root DIR]
```

Ejecutado internamente como:

```bash
python3 tools/aris4u_init.py [opciones]
# o equivalentemente:
python3 -m tools.aris4u_init [opciones]
```

**Parámetros:**

| Flag | Descripción |
|------|-------------|
| `--yes` / `-y` | Modo no interactivo: incluye todos los repos detectados sin preguntar |
| `--dry-run` | Imprime el JSON resultante en stdout sin escribir en `~/.aris4u/config.json` |
| `--force` | Re-inicializa ignorando la config existente (no fusiona, recomputa todo) |
| `--scan-root DIR` | Directorio raíz de proyectos a escanear (default: `~/projects`) |

**Variable de entorno:**
- `ARIS4U_CONFIG`: override de la ruta destino del config (default: `~/.aris4u/config.json`).

**Ejemplos:**

```bash
# Primera instalación — no interactivo
python3 tools/aris4u_init.py --yes

# Ver qué generaría sin escribir nada
python3 tools/aris4u_init.py --dry-run --yes

# Forzar reset completo
python3 tools/aris4u_init.py --yes --force

# Escanear directorio alternativo
python3 tools/aris4u_init.py --yes --scan-root ~/code
```

## Execution Flow

1. **Resolver ruta de config**
   - Lee `$ARIS4U_CONFIG`; si vacío, usa `~/.aris4u/config.json`.

2. **Cargar config existente** (si existe y no se usa `--force`)
   - `json.loads(~/.aris4u/config.json)` → dict.
   - Claves a preservar: `owner`, `ollama_*`, `w2_*`, `clients`,
     `healthcare_clients`, y cualquier clave custom del usuario.

3. **Auto-detectar hardware** (siempre se recomputa)
   - `os.cpu_count()` → cores.
   - `platform.machine()` / `platform.system()` → arch / OS.
   - RAM: `psutil.virtual_memory()` → fallback `sysctl hw.memsize` (Darwin)
     o `/proc/meminfo` (Linux). Timeout 2 s.
   - Chip: `sysctl machdep.cpu.brand_string` (Intel) o `system_profiler SPHardwareDataType`
     (Apple Silicon) o `lscpu` (Linux). Timeout 2-5 s.
   - GPU: `nvidia-smi --query-gpu=name,memory.total` (opcional). Timeout 3 s.
   - Todo envuelto en `try/except Exception` — nunca lanza.

4. **Escanear proyectos** (siempre se recomputa)
   - Recorre `~/projects/` y `~/projects/03-clients/` buscando dirs con `.git/`.
   - Detecta marcadores `.aris-client` existentes (topic = "aris-client").
   - Con `--yes`: incluye todos. Sin `--yes`: confirma cada uno con `input()`.

5. **Detectar clientes**
   - Lista subdirs de `~/projects/03-clients/`.
   - Healthcare inferido por keywords en el nombre del dir:
     `health`, `medical`, `hospital`, `radiology`, `phi`.

6. **Fusión**
   - Claves `hardware` y `lab_projects`: siempre del paso 3-4 (recomputadas).
   - Resto de claves: el valor del config EXISTENTE tiene precedencia
     (preserva `owner`, `ollama_mac_url`, etc.).
   - Con `--force`: se usa la config nueva sin preservar nada.

7. **Escribir / dry-run**
   - Dry-run: imprime JSON a stdout + separador `---` + resumen.
   - Escritura: `~/.aris4u/` creado con `os.makedirs(exist_ok=True)`;
     archivo con `chmod 0o600`; re-lectura de validación post-escritura.

8. **Imprimir resumen**
   - Owner, hardware (label/cores/RAM/arch), # labs, clients, healthcare, ruta dest.
   - Exit 0 si el JSON re-parseó OK; exit 1 si la validación post-escritura falla.

## Output Schema

El `~/.aris4u/config.json` generado contiene:

```json
{
  "_comment": "Config generada por 'aris4u init'. NO versiones este archivo.",
  "owner": "Tu Nombre",
  "hardware": {
    "auto_detect": true,
    "primary": {
      "label": "Apple M5 Pro",
      "cores": 18,
      "arch": "arm64",
      "platform": "Darwin",
      "ram_gb": 48.0
    },
    "workers": [
      {"name": "w2", "ssh": "w2", "gpu": "RTX 3070 Laptop (8 GB)", "enabled": true}
    ],
    "dead": []
  },
  "lab_projects": [
    {"dir": "my-lab", "db_project": "my-lab", "topic": "lab", "path": "~/projects/my-lab"}
  ],
  "clients": ["acme-corp", "my-client"],
  "healthcare_clients": ["my-health-client"],
  "ollama_mac_url": "http://localhost:11434",
  "ollama_w2_url": "http://YOUR_W2_TAILSCALE_IP:11434",
  "w2_ssh": "w2",
  "w2_enabled": true
}
```

## Quality Gates

- **Idempotencia**: correr 2 veces sobre el mismo directorio da el mismo resultado.
- **No destructivo**: nunca sobrescribe claves del usuario (ollama_*, w2_*, owner) sin `--force`.
- **Dry-run seguro**: `--dry-run` garantiza CERO escrituras en disco.
- **Validación post-escritura**: re-lee el archivo escrito con `json.loads`; exit 1 si falla.
- **Permisos**: `~/.aris4u/config.json` siempre queda con `chmod 0o600` (solo el usuario).

## Notes

- **`hardware` y `lab_projects` siempre se recomputan** (incluso sin `--force`) porque
  son auto-detected. Las claves de usuario (conexiones, nombres) siempre se preservan.
- El archivo vive en `~/.aris4u/` que está en `.gitignore` — nunca se versiona.
- El skill NO toca `hooks/`, `engine/v16/config.py`, ni `~/.claude/settings.json`.
- Para ver la config actual resultante: `python3 tools/aris4u_init.py --dry-run --yes`.
- Tests en `tests/tools/test_aris4u_init.py` cubren dry-run, hardware, merge y idempotencia.

## Integration with ARIS4U Workflow

```
# Instalar ARIS4U en una máquina nueva:
git clone <repo> ~/projects/aris4u
cd ~/projects/aris4u
python3 -m venv .venv312 && source .venv312/bin/activate
pip install -e ".[dev]"
python3 tools/aris4u_init.py --yes     # genera ~/.aris4u/config.json
# → ARIS4U ya puede leer hardware, proyectos y URLs de Ollama
```

---

**Version:** v1.0
**Status:** Production
**Last Updated:** 2026-06-30
