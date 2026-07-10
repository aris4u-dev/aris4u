import os
from pathlib import Path

# Portabilidad (Fase 0): derivar de la ubicación del propio módulo
# (engine/v16/config.py → parents[2] = raíz del repo), con override por entorno
# para empaquetado como plugin (${CLAUDE_PLUGIN_ROOT}) y uso multi-usuario.
ARIS4U_ROOT = Path(os.environ.get("ARIS4U_ROOT") or Path(__file__).resolve().parents[2])
DATA_DIR = ARIS4U_ROOT / "data"
HOOKS_DIR = ARIS4U_ROOT / "hooks"

SESSIONS_DB = DATA_DIR / "sessions.db"
# CLAUDE_MEM_DB retirado en V18 Fase E (paso 10): memoria desacoplada a observations_local.

# Config por-usuario (Paso 6): overrides desde ~/.aris4u/config.json (o $ARIS4U_CONFIG)
# y variables de entorno, con fallback a estos defaults. Permite a otro dev ajustar
# endpoints o desactivar W2 SIN tocar código. Prioridad: env var > config.json > default.
def _load_user_config() -> dict:
    import json
    try:
        p = Path(os.environ.get("ARIS4U_CONFIG") or (Path.home() / ".aris4u" / "config.json"))
        if p.is_file():
            return json.loads(p.read_text())
    except Exception:
        pass
    return {}


_USER_CFG = _load_user_config()


def _cfg(key: str, env: str, default):
    """Resuelve un valor de config: env var > ~/.aris4u/config.json > default."""
    v = os.environ.get(env)
    if v is not None:
        return v
    return _USER_CFG.get(key, default)


OLLAMA_MAC_URL = _cfg("ollama_mac_url", "ARIS4U_OLLAMA_MAC_URL", "http://localhost:11434")
OLLAMA_W2_URL = _cfg("ollama_w2_url", "ARIS4U_OLLAMA_W2_URL", "http://YOUR_W2_IP:11434")
W2_SSH = _cfg("w2_ssh", "ARIS4U_W2_SSH", "w2")
# W2 (worker remoto) opcional: un dev sin W2 lo desactiva y todo degrada limpio.
W2_ENABLED = str(_cfg("w2_enabled", "ARIS4U_W2_ENABLED", "true")).strip().lower() in ("1", "true", "yes", "on")

# Cuerpo local: Mistral-Small-3.2-24B (denso, Mistral AI/Francia) servido por mlx_lm.server.
# LAZY: el server se arranca aparte (tools/mlx_serve.sh); si NO corre, dispatch_mlx devuelve
# None y el router cae a Foundation-Sec/W2 (fail-open, health-aware). ~12GB en 4bit → cede
# memoria a Claude cuando no se usa (anti-saturación).
# SWAP 2026-07-01 (Qwen3.6-35B-A3B, Alibaba/China → Mistral, occidental): política anti-IA-china
# del dueño. Benchmark M5: 3/3 pruebas correctas (código Dart/Python + razonamiento ES) a ~20 tok/s.
# Descartados con evidencia: Nemotron-Cascade-2-4bit (loops/roto), gpt-oss-20b (2/3, se enreda en
# código). Denso > MoE aquí por FIABILIDAD para hooks automáticos (structure/critique/digest).
MLX_MODEL = _cfg("mlx_model", "ARIS4U_MLX_MODEL", "mlx-community/Mistral-Small-3.2-24B-Instruct-2506-4bit")
MLX_URL = _cfg("mlx_url", "ARIS4U_MLX_URL", "http://localhost:8765")

MAC_MODELS = {
    # 2026-06-18 V2.0 Fase 3a: los 4 generativos previos (qwen2.5:7b-instruct,
    # qwen35-analyst, qwen35-pentester, gemma4-abliterated) fueron BORRADOS el 06-16.
    # Reapuntados a lo VIVO: Foundation-Sec-8B (único generativo Mac instalado; modelo
    # de seguridad — encaja con analyst/pentester bajo el framing ofensivo). Fase 3b
    # instalará Qwen3.6-35B-A3B (MLX) como generativo Mac primario y lo reemplazará aquí.
    # Esto es solo la red de seguridad de dispatch_local; el router pasa modelo explícito.
    "default": "hf.co/roadus/Foundation-Sec-8B-Q4_K_M-GGUF:latest",
    "analyst": "hf.co/roadus/Foundation-Sec-8B-Q4_K_M-GGUF:latest",
    "pentester": "hf.co/roadus/Foundation-Sec-8B-Q4_K_M-GGUF:latest",
    "fallback": "hf.co/roadus/Foundation-Sec-8B-Q4_K_M-GGUF:latest",
    "embed": "mxbai-embed-large:latest",
}

W2_MODELS = {
    "pentesting": "xploiter/the-xploiter:latest",
    "security": "hf.co/roadus/Foundation-Sec-8B-Q4_K_M-GGUF:latest",
    "compute": "qwen3:8b",
}

DEPTH_LEVELS = {
    "simple": [1],
    "fix": [1, 5, 7, 9],
    "decision": [1, 2, 3, 4, 5, 6],
    "research": [1, 2, 3, 4, 5, 6],
    "implementation": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
}

BUSY_TIMEOUT_MS = 10000
MAX_FTS_RESULTS = 5
MAX_DIGEST_LENGTH = 2000

# Mejora #4 — boost de átomos en auto_recall (2026-06-23).
# Los átomos de método (mem_type='fact' con problem_class o structural_signature) son
# CONOCIMIENTO accionable (patrones reutilizables), no bitácora. Compiten en el mismo
# KNN semántico que la memoria general y a menudo quedan fuera del top-3 por similitud
# cruda → no se activan solos. El boost les reserva su propio canal con slots dedicados,
# alimentado por un pool semántico más ancho (RECALL_POOL_LIMIT). UN solo embed (split del
# mismo pool, sin coste de latencia extra sobre el cap SIGALRM 2s del hook).
RECALL_POOL_LIMIT = int(os.getenv("ARIS4U_RECALL_POOL", "8"))   # pool semántico a sobre-pedir
ATOM_RECALL_LIMIT = int(os.getenv("ARIS4U_ATOM_RECALL_LIMIT", "2"))  # slots reservados a átomos
ATOM_RECALL_MIN_SIM = float(os.getenv("ARIS4U_ATOM_RECALL_MIN_SIM", "0.35"))  # piso anti-ruido

# Inyección de SKELETON al build flow (2026-06-23): cuando la intención es construir/arreglar
# y el átomo top es MUY relevante, se inyecta su plantilla de código (skeleton) bajo el átomo
# para que la implementación probada esté a la mano — el conocimiento se APLICA, no solo se ve.
# Piso más alto que el recall normal (anti-ruido: una plantilla equivocada estorba más que falta).
SKELETON_INJECT_INTENTS = frozenset({"implementation", "fix"})
SKELETON_INJECT_MIN_SIM = float(os.getenv("ARIS4U_SKELETON_MIN_SIM", "0.5"))
SKELETON_MAX_LINES = int(os.getenv("ARIS4U_SKELETON_MAX_LINES", "24"))  # recorte anti-bloat de contexto

# WS3 — Semantic vector substrate (sqlite-vec sidecar, ARIS4U-owned)
# Sidecar DB keeps claude-mem.db / sessions.db immutable; ARIS4U owns the index.
# Embeddings via local Mac Ollama (PHI-safe, no external API).
ARIS_VECTORS_DB = DATA_DIR / "aris_vectors.db"
# SWAP 2026-07-01 (bge-m3 BAAI/China → EmbeddingGemma Google/USA): política anti-IA-china
# del dueño. A/B en el índice REAL (sqlite-vec, prefijos cableados, mismo método): Gemma gana a
# bge-m3 en todo — @1 0.593 vs 0.507, @5 0.68 vs 0.64, MRR 0.637 vs 0.565. Reindex no-destructivo
# vía tools/reindex_embeddings.py (9763/9765 OK); backup del chino en data/aris_vectors.bge-m3.bak.
# EmbeddingGemma: 768d, multilingüe, EXIGE prefijos de tarea (ver EMBED_PREFIX).
EMBED_MODEL = "embeddinggemma"
EMBED_DIM = 768
# Prefijos de tarea por modelo (query vs documento). Modelos asimétricos (EmbeddingGemma,
# arctic) EXIGEN estos prefijos o el recall cae; bge-m3/mxbai no los usan. embed_text los
# aplica según (EMBED_MODEL, role). A/B verificado 2026-07-01 (evals/ab_embedders_western.py).
EMBED_PREFIX = {
    "bge-m3": {"query": "", "doc": ""},
    "mxbai-embed-large": {"query": "Represent this sentence for searching relevant passages: ", "doc": ""},
    "embeddinggemma": {"query": "task: search result | query: ", "doc": "title: none | text: "},
    "snowflake-arctic-embed2": {"query": "query: ", "doc": ""},
}
VECTOR_DEFAULT_K = 5
NO_CLIENT_SENTINEL = ""  # vec0 TEXT metadata rejects NULL; "" means unscoped

def _read_plugin_version() -> str:
    """Versión canónica = la del plugin.json (fuente única), no un literal stale."""
    try:
        import json
        return json.loads((ARIS4U_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
    except Exception:
        return "16.9.0"  # fallback si el manifest no es legible


ARIS4U_VERSION = _read_plugin_version()
# Modelo activo: override por entorno; en Fase 2 esto se moverá a models_manifest.json.
# El default debe ser un ID válido para /v1/messages/count_tokens (antes: stale 4-7).
CLAUDE_MODEL = os.environ.get("ARIS4U_CLAUDE_MODEL", "claude-opus-4-8")
PROMPT_CACHE_TTL = "1h"

# Auto-adapt mode (Tramo 3 §7): off | shadow | pr-only | auto
#   off     : kill-switch — does nothing.
#   shadow  : detect + log what WOULD happen; baseline never updated. (default)
#   pr-only : on change + gate PASS → open PR (branch + commit + gh pr create). NEVER auto-merge.
#   auto    : RESERVED — not implemented yet (Tramo 4).
# Override via env: ARIS4U_AUTOUPDATE=pr-only; or set "autoupdate_mode" in ~/.aris4u/config.json.
AUTOUPDATE_MODE = _cfg("autoupdate_mode", "ARIS4U_AUTOUPDATE", "shadow")
SUBAGENT_DEPTH_HOOK = HOOKS_DIR / "subagent_depth.sh"
PLUGIN_DIR = ARIS4U_ROOT / ".claude-plugin"

WAVE_DURATION_MINUTES = int(os.getenv("ARIS4U_WAVE_MINUTES", "90"))
TOKEN_WARN_THRESHOLD_PCT = int(os.getenv("ARIS4U_TOKEN_WARN_PCT", "70"))
# Calibrado empíricamente 2026-06-04 (el usuario): el auto-compact de Opus 4.8 1M disparó
# con el estimador acumulado en ~966k → 966k = 100% del indicador (antes 200k, era 4.7).
# El estimador se resetea a 0 en cada renovación de ventana (SessionStart != resume).
TOKEN_BUDGET_MAX_TOKENS = int(os.getenv("ARIS4U_TOKEN_BUDGET_MAX", "966000"))
TOKEN_ESTIMATE_RATIO = 4
TOKEN_ESTIMATE_PROMPT_PREFIX = 5000

# Session-level wave tracking (populated at runtime)
# Format: session_state['wave_start_time'] = datetime.now()
# Format: session_state['accumulated_token_estimate'] = sum of all prompt lengths / TOKEN_ESTIMATE_RATIO
# Format: session_state['query_count'] = count of user queries in current wave

# Contract Guard Configuration
CONTRACT_GUARD_ENABLED = True
CONTRACT_GUARD_STATE = Path.home() / ".aris4u" / "contract_guard_state.json"

CONTRACT_MODULE_PATHS = [
    r"lib/features/",
    r"lib/src/",
    r"src/main/",
    r"src/components/",
    r"src/modules/",
    r"engine/v16/",
    r"modules/",
    r"components/",
]

CONTRACT_EXEMPT_PATHS = [
    r"\.planning/",
    r"\.claude",
    r"/tests?/",
    r"_test\.[^/]+$",
    r"/test_[^/]+$",
    r"\.md$",
    r"CLAUDE\.md",
    r"config\.",
    r"\.json$",
    r"\.yaml$",
    r"\.yml$",
    r"\.lock$",
    r"pubspec\.",
    r"package\.",
    r"\.gitignore",
    r"analysis_options",
    r"\.g\.dart$",
    r"\.freezed\.dart$",
]

CONTRACT_BUILD_PATTERNS = [
    r"build\s+(me\s+)?",
    r"implement\s+",
    r"create\s+(new\s+)?(module|feature|screen|page)",
    r"add\s+(new\s+)?(module|feature|screen|page)",
    r"construye\s+",
    r"implementa\s+",
    r"crea\s+(nuevo\s+)?(modulo|feature)",
]

# Adaptive Depth (ACT-based per-query level adjustment)
ADAPTIVE_DEPTH_MIN_COMPLEXITY = 25
ADAPTIVE_DEPTH_ENABLED = True

# Token Intelligence (effort routing + terse mode)
EFFORT_LEVEL_ENABLED = True
EFFORT_LEVEL_MAPPING = {
    "simple": "low",
    "fix": "medium",
    "decision": "high",
    "implementation": "xhigh",
}
TERSE_THRESHOLD_PCT = 65


# ---------------------------------------------------------------------------
# Config accessors — leen _USER_CFG con defaults hardcodeados (fail-open)
# ---------------------------------------------------------------------------

def cfg_owner() -> str:
    """Devuelve el nombre del dueño de la instancia desde config; 'el usuario' si ausente.

    Returns:
        Nombre del dueño (str), nunca vacío.
    """
    return str(_USER_CFG.get("owner") or "el usuario")


def cfg_lab_projects() -> list[dict]:
    """Lista de proyectos-lab desde config.json (campo 'lab_projects').

    Returns:
        Lista de dicts [{name, path, ...}] o [] si no configurado.
    """
    return list(_USER_CFG.get("lab_projects") or [])


def cfg_clients() -> list[str]:
    """Lista de IDs de clientes activos desde config.json (campo 'clients').

    Returns:
        Lista de strings o [] si no configurado.
    """
    return list(_USER_CFG.get("clients") or [])


def cfg_healthcare_clients() -> list[str]:
    """IDs de clientes healthcare desde config.json; vacío si no configurado.

    Configura en ~/.aris4u/config.json:
        {"healthcare_clients": ["cliente-medico-a", "cliente-medico-b"]}

    Returns:
        Lista de strings o [] si no configurado (config ausente ⇒ comportamiento genérico).
    """
    configured = _USER_CFG.get("healthcare_clients")
    if configured is not None:
        return list(configured)
    return []


# ---------------------------------------------------------------------------
# Hardware detection — portable, fail-open, nunca crashea
# ---------------------------------------------------------------------------

def detect_hardware_block() -> str:
    """Detecta el hardware en tiempo de ejecución de forma portable.

    Intenta obtener: CPU count, arquitectura/plataforma, RAM (psutil → sysctl → /proc),
    chip (sysctl machdep.cpu.brand_string en Darwin), GPU (nvidia-smi en Linux/Win).
    Todos los pasos son fail-open (try/except Exception); campos ausentes → '?' u omitidos.

    Returns:
        Bloque de texto multi-línea listo para incrustar en briefing.
    """
    import platform
    import subprocess as _sp

    lines: list[str] = []

    # CPU
    try:
        cpu_count = os.cpu_count() or "?"
        arch = platform.machine() or "?"
        system = platform.system() or "?"
        lines.append(f"  • CPU: {cpu_count} cores · arch={arch} · {system}")
    except Exception:
        lines.append("  • CPU: ?")

    # RAM
    ram_str = "?"
    try:
        import psutil  # type: ignore[import]
        total_gb = psutil.virtual_memory().total / (1024 ** 3)
        ram_str = f"{total_gb:.0f}GB"
    except Exception:
        try:
            sys_name = platform.system()
            if sys_name == "Darwin":
                r = _sp.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0 and r.stdout.strip():
                    total_gb = int(r.stdout.strip()) / (1024 ** 3)
                    ram_str = f"{total_gb:.0f}GB"
            elif sys_name == "Linux":
                with open("/proc/meminfo") as _f:
                    for _line in _f:
                        if _line.startswith("MemTotal:"):
                            kb = int(_line.split()[1])
                            ram_str = f"{kb / (1024 ** 2):.0f}GB"
                            break
        except Exception:
            pass
    lines.append(f"  • RAM: {ram_str}")

    # Chip (Darwin-only, best-effort)
    try:
        if platform.system() == "Darwin":
            r = _sp.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True, timeout=2,
            )
            chip = (r.stdout.strip() or "?") if r.returncode == 0 else "?"
            lines.append(f"  • Chip: {chip}")
    except Exception:
        pass

    # GPU via nvidia-smi (opcional, Linux/Win)
    try:
        r = _sp.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            gpu_line = r.stdout.strip().splitlines()[0]
            lines.append(f"  • GPU: {gpu_line}")
    except Exception:
        pass

    return "\n".join(lines) if lines else "  • (hardware no detectado)"


def _build_workers_suffix(hw: dict) -> str:
    """Genera las líneas de workers remotos desde la sección hardware de la config.

    Itera ``hw["workers"]`` e incluye una línea por cada worker enabled:
    ``  • <name>: <gpu> — <note>`` (omite gpu/note si están ausentes).
    Genérico: funciona con cualquier nombre de worker, sin hardcodear ninguno.
    Fail-open: cualquier excepción o campo ausente → devuelve "".

    Args:
        hw: Sección "hardware" del diccionario de config (puede ser {}).

    Returns:
        String con las líneas de workers (precedidas de "\\n") o "" si no hay.
    """
    try:
        workers = hw.get("workers") or []
        lines: list[str] = []
        for w in workers:
            if not isinstance(w, dict):
                continue
            if not w.get("enabled", True):
                continue
            name = str(w.get("name") or "worker")
            gpu = str(w.get("gpu") or "")
            note = str(w.get("note") or "")
            line = f"  • {name}"
            if gpu:
                line += f": {gpu}"
            if note:
                line += f" — {note}"
            lines.append(line)
        if lines:
            return "\n" + "\n".join(lines)
    except Exception:
        pass
    return ""


def build_hardware_block(cfg: dict) -> str:
    """Resuelve el bloque de hardware desde config, estructura o detección automática.

    Prioridad:
      1. cfg["hardware"]["block"] (str) → devolverlo tal cual (override completo).
      2. cfg["hardware"]["primary"] (str) → formatear con prefijo + workers.
      3. Fallback → detect_hardware_block() + workers (detección dinámica).

    Tras el primary (o el fallback detectado), añade una línea por cada worker
    enabled en cfg["hardware"]["workers"]: "• <name>: <gpu> — <note>".
    Fail-open: sin workers en config → no añade nada (comportamiento actual).
    Genérico: cualquier nombre de worker, sin hardcodear ninguno.

    Args:
        cfg: Diccionario de config (típicamente _USER_CFG).

    Returns:
        Bloque de texto listo para incrustar en el briefing.
    """
    hw = cfg.get("hardware") or {}
    workers_suffix = _build_workers_suffix(hw) if isinstance(hw, dict) else ""
    if isinstance(hw, dict):
        block = hw.get("block")
        if block and isinstance(block, str):
            return block  # override completo: no añadir workers (el bloque ya los incluye)
        primary = hw.get("primary")
        if primary and isinstance(primary, str):
            return f"  • {primary}" + workers_suffix
    return detect_hardware_block() + workers_suffix
