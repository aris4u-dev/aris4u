#!/usr/bin/env python3
"""Inventario en vivo del 100% de capacidades de Claude/ARIS4U (read-only).

Paso 1 del Enrutador de Capacidades (``architecture/CAPABILITY_ROUTER_PLAN.md`` §5.1).

Enumera, desde las fuentes REALES de disco/config, todas las capacidades que Claude
puede usar y las estructura en un inventario re-ejecutable:

  - skills    -> ~/.claude/skills/*/SKILL.md            (usuario)
  - agents    -> ~/.claude/agents/*.md                  (usuario)
  - commands  -> ~/.claude/commands/**/*.md             (usuario; surgen como skills en runtime)
  - plugins   -> ~/.claude/plugins/installed_plugins.json -> installPath/{skills,agents,commands}
  - mcp_server-> ~/.claude.json + settings.json + repo .mcp.json
  - mcp_tool  -> integrations/mcp_server.py (@mcp.tool())  [solo el servidor aris4u]
  - hook      -> ~/.claude/settings.json (hooks por evento)

Reconciliación disco <-> runtime: el harness inyecta en CADA sesión un set de
capacidades que NO viven en disco (MCP empresariales claude_ai_*, agentes/skills
*built-in* como Explore/Plan//verify). Para medir el "100%" real se carga, si existe,
``data/capability_runtime_snapshot.json`` (el set autoritativo de ESTA sesión) y se
calcula la cobertura: cuántas capacidades del runtime están respaldadas en disco y
cuántas son runtime-only. Disciplina SUBSET-FULL: todo se reporta como ``N de TOTAL``.

Alcance: el escaneo cubre la config GLOBAL del usuario (~/.claude) + los plugins
instalados + el repo aris4u. La config por-PROYECTO (``<proyecto>/.claude/{commands,
settings.json}``) NO se escanea: solo está activa cuando Claude corre dentro de ese
cwd, y el runtime_snapshot de esta sesión tampoco la contendría. Esa capa se resuelve
en runtime, no en este inventario.

Seguridad: solo LEE archivos de config y parsea fuentes. Nunca escribe nada de
producción (mismo principio que ``tools/aris_status.py``).

Uso:
    python3 tools/capability_inventory.py            # panel resumido
    python3 tools/capability_inventory.py --json     # inventario estructurado completo
    python3 tools/capability_inventory.py --no-color
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

import yaml  # PyYAML 6.x (presente en .venv312)

HOME = Path.home()
ARIS_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_DIR = HOME / ".claude"
USER_SKILLS = CLAUDE_DIR / "skills"
USER_AGENTS = CLAUDE_DIR / "agents"
USER_COMMANDS = CLAUDE_DIR / "commands"
USER_WORKFLOWS = CLAUDE_DIR / "workflows"
SETTINGS = CLAUDE_DIR / "settings.json"
CLAUDE_JSON = HOME / ".claude.json"
PLUGINS_DB = CLAUDE_DIR / "plugins" / "installed_plugins.json"
REPO_MCP = ARIS_ROOT / ".mcp.json"
MCP_SERVER_SRC = ARIS_ROOT / "integrations" / "mcp_server.py"
RUNTIME_SNAPSHOT = ARIS_ROOT / "data" / "capability_runtime_snapshot.json"
# Semilla genérica versionada (sin datos del usuario): el snapshot VIVO es per-máquina y
# se .gitignora (contiene el toolkit del dueño); el repo solo distribuye esta semilla, que
# aporta los campos no-escaneables (mcp_tools de servers externos, builtin_tools). Si el
# snapshot vivo no existe (instalación fresca), se cae a la semilla.
SEED_SNAPSHOT = ARIS_ROOT / "data" / "capability_runtime_snapshot.seed.json"
HOOKS_DEPTH = ARIS_ROOT / "data" / "aris4u_hooks_depth.json"


def _load_snapshot_or_seed() -> dict[str, Any]:
    """Carga el snapshot vivo si existe; si no, la semilla genérica versionada (fail-open {})."""
    data = _load_json(RUNTIME_SNAPSHOT)
    if data:
        return data
    return _load_json(SEED_SNAPSHOT)

# Categorías cuyas capacidades el runtime expone como "skills" (incluye comandos).
_SKILL_LIKE = {"skill", "command"}

# --------------------------------------------------------------------------- #
# Seeds nativas del harness de Claude Code
# Capacidades built-in que no viven en disco (~/.claude/skills/, plugins, commands)
# y que el harness inyecta en CADA sesión sin instalación explícita.
# Se usan en build_live_snapshot() para que el inventario cubra el 100% del toolkit.
# Actualizar cuando Claude Code añada o elimine skills/agents nativos.
# --------------------------------------------------------------------------- #
NATIVE_HARNESS_SKILLS: list[str] = [
    "claude-api",       # Referencia de la API de Claude / Anthropic SDK
    "claude-in-chrome", # Automatización de Chrome con browser tools
    "code-review",      # Revisión de diff para bugs y cleanup
    "deep-research",    # Fan-out de búsquedas web con síntesis citada
    "fewer-permission-prompts",  # Optimización de permisos via allowlist
    "init",             # Inicializar CLAUDE.md con documentación del codebase
    "keybindings-help", # Customizar atajos de teclado (~/.claude/keybindings.json)
    "loop",             # Ejecutar un comando en intervalo recurrente
    "review",           # Revisar un pull request
    "run",              # Lanzar y observar la app del proyecto
    "schedule",         # Programar mensajes en la sesión activa
    "security-review",  # Revisión de seguridad del branch actual
    "simplify",         # Revisión de reuse/simplification del código cambiado
    "update-config",    # Configurar el harness de Claude Code via settings.json
    "verify",           # Verificar que un cambio funciona corriendo la app
]

NATIVE_HARNESS_AGENTS: list[str] = [
    "claude",           # Catch-all / agente por defecto
    "claude-code-guide",# Guía de Claude Code (features, hooks, MCP, settings)
    "Explore",          # Búsqueda read-only rápida de código
    "general-purpose",  # Agente de propósito general
    "Plan",             # Arquitecto de software (planes de implementación)
    "statusline-setup", # Configurar el statusline de Claude Code
]

# Listas de tools conocidas para MCP servers de infraestructura (account-level).
# Se usan en build_live_snapshot() para poblar mcp_tools de servers que no están
# en el snapshot existente y cuyas tools no se pueden descubrir dinámicamente.
# Formato: { server_name: [tool_name, ...] } (ordenado alfabéticamente).
NATIVE_MCP_TOOL_SEEDS: dict[str, list[str]] = {
    "cloudflare-builds": ["authenticate", "complete_authentication"],
}


@dataclass
class Capability:
    """Una capacidad enumerada (skill/agent/command/mcp_server/mcp_tool/hook)."""

    name: str
    ctype: str
    invocation: str
    source: str
    description: str = ""
    defined_at: str | None = None
    namespace: str | None = None
    available: bool | None = None  # respaldada por el snapshot de runtime
    status: str = "defined"  # señal barata del paso 1; el paso 2 mide liveness real
    liveness: str = "unknown"  # paso 2: live | broken | dormant | external
    extra: dict[str, Any] = field(default_factory=dict)

    def runtime_keys(self) -> list[str]:
        """Claves candidatas para casar esta capacidad contra el set de runtime."""
        keys = self.extra.get("runtime_keys")
        return list(keys) if keys else [self.name]


# --------------------------------------------------------------------------- #
# Utilidades de lectura (read-only, fail-open)
# --------------------------------------------------------------------------- #
def _load_json(path: Path) -> dict[str, Any]:
    """Lee un JSON; devuelve {} ante cualquier fallo (fail-open)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_frontmatter(path: Path) -> dict[str, Any]:
    """Extrae el frontmatter YAML (bloque entre ``---``) de un .md.

    Args:
        path: Ruta al archivo markdown.

    Returns:
        Diccionario con los campos del frontmatter, o {} si no hay/está roto.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _norm(text: Any) -> str:
    """Colapsa espacios en blanco de una descripción a una sola línea limpia."""
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return " ".join(text.split())


def _first_para(path: Path) -> str:
    """Primera línea de prosa (no frontmatter, no encabezado) como fallback de desc."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    text = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, count=1, flags=re.DOTALL)
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return _norm(line)[:280]
    return ""


# --------------------------------------------------------------------------- #
# Escáneres por fuente
# --------------------------------------------------------------------------- #
def scan_user_skills() -> list[Capability]:
    """Skills del usuario en ~/.claude/skills/*/SKILL.md."""
    caps: list[Capability] = []
    for sk in sorted(USER_SKILLS.glob("*/SKILL.md")):
        fm = _read_frontmatter(sk)
        name = str(fm.get("name") or sk.parent.name)
        caps.append(
            Capability(
                name=name,
                ctype="skill",
                invocation=f"/{name}",
                source="user",
                description=_norm(fm.get("description")) or _first_para(sk),
                defined_at=str(sk),
            )
        )
    return caps


def scan_user_agents() -> list[Capability]:
    """Subagentes del usuario en ~/.claude/agents/*.md."""
    caps: list[Capability] = []
    for ag in sorted(USER_AGENTS.glob("*.md")):
        fm = _read_frontmatter(ag)
        name = str(fm.get("name") or ag.stem)
        caps.append(
            Capability(
                name=name,
                ctype="agent",
                invocation=f"Agent(subagent_type='{name}')",
                source="user",
                description=_norm(fm.get("description")),
                defined_at=str(ag),
                extra={"tools": fm.get("tools"), "model": fm.get("model")},
            )
        )
    return caps


def scan_user_commands() -> list[Capability]:
    """Comandos del usuario en ~/.claude/commands/**/*.md (runtime los muestra como skills)."""
    caps: list[Capability] = []
    if not USER_COMMANDS.is_dir():
        return caps
    for cmd in sorted(USER_COMMANDS.rglob("*.md")):
        parts = list(cmd.relative_to(USER_COMMANDS).with_suffix("").parts)
        if not parts:
            continue
        name = ":".join(parts)
        ns = parts[0] if len(parts) > 1 else None
        fm = _read_frontmatter(cmd)
        caps.append(
            Capability(
                name=name,
                ctype="command",
                invocation=f"/{name}",
                source="user",
                description=_norm(fm.get("description")) or _first_para(cmd),
                defined_at=str(cmd),
                namespace=ns,
            )
        )
    return caps


def _workflow_desc(path: Path) -> str:
    """Primera línea de comentario de un archivo .js como descripción (fail-open).

    Args:
        path: Ruta al archivo .js del workflow.

    Returns:
        Primera línea significativa de comentario (// o /*), normalizada y
        truncada a 280 chars; cadena vacía si no hay comentarios.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            return _norm(stripped.lstrip("/").strip())[:280]
        if stripped.startswith("/*"):
            return _norm(stripped.lstrip("/*").strip())[:280]
    return ""


def scan_user_workflows() -> list[Capability]:
    """Workflows del usuario en ~/.claude/workflows/*.js.

    Los workflows se exponen en runtime como skills invocables (``/name``).
    A diferencia de skills y commands, no tienen frontmatter YAML; la descripción
    se extrae del primer comentario del archivo.

    Returns:
        Lista de capacidades con ctype='skill' y source='user:workflow'.
    """
    caps: list[Capability] = []
    if not USER_WORKFLOWS.is_dir():
        return caps
    for wf in sorted(USER_WORKFLOWS.glob("*.js")):
        name = wf.stem
        caps.append(
            Capability(
                name=name,
                ctype="skill",
                invocation=f"/{name}",
                source="user:workflow",
                description=_workflow_desc(wf),
                defined_at=str(wf),
            )
        )
    return caps


def _plugin_items(install_path: Path, subglob: str) -> list[Path]:
    """Glob recursivo dedup'd dentro de un installPath (evita doble-conteo)."""
    seen: set[Path] = set()
    out: list[Path] = []
    for p in sorted(install_path.rglob(subglob)):
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        out.append(p)
    return out


# (tipo, subglob) por item que un plugin puede aportar.
_PLUGIN_ITEM_TYPES = (("skill", "SKILL.md"), ("agent", "agents/*.md"), ("command", "commands/*.md"))


def _plugin_cap(path: Path, ns: str, key: str, scope: str, ctype: str) -> Capability:
    """Construye una Capability para un item (skill/agent/command) de un plugin."""
    fm = _read_frontmatter(path)
    base = path.parent.name if ctype == "skill" else path.stem
    nm = str(fm.get("name") or base)
    full = f"{ns}:{nm}"
    extra: dict[str, Any] = {"scope": scope, "runtime_keys": [full, f"{ns}:{base}"]}
    description = _norm(fm.get("description")) or _first_para(path)
    if ctype == "agent":
        extra["tools"] = fm.get("tools")
        invocation = f"Agent(subagent_type='{full}')"
    else:
        invocation = f"/{full}"
    return Capability(
        name=full,
        ctype=ctype,
        invocation=invocation,
        source=f"plugin:{key}",
        description=description,
        defined_at=str(path),
        namespace=ns,
        extra=extra,
    )


def scan_plugins() -> list[Capability]:
    """Skills/agents/commands aportados por los plugins instalados.

    Fuente de verdad = ~/.claude/plugins/installed_plugins.json (qué está instalado
    y en qué installPath de cache/), NO los clones crudos en marketplaces/.
    """
    caps: list[Capability] = []
    db = _load_json(PLUGINS_DB)
    for key, insts in (db.get("plugins") or {}).items():
        if not insts:
            continue
        inst = insts[0]
        ns = key.split("@")[0]
        scope = inst.get("scope", "")
        ip = Path(inst.get("installPath", ""))
        if not ip.is_dir():
            continue
        for ctype, sub in _PLUGIN_ITEM_TYPES:
            for path in _plugin_items(ip, sub):
                # agents/ y commands/ anidados DENTRO de skills/<x>/ son sub-pasos
                # internos de esa skill, no capacidades invocables del plugin.
                if ctype in ("agent", "command") and "skills" in path.parts:
                    continue
                caps.append(_plugin_cap(path, ns, key, scope, ctype))
    return caps


def _mcp_from(mapping: Any, label: str) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Emite (servidor, label, config) de un mapa mcpServers (config saneada a dict)."""
    for n, cfg in (mapping or {}).items():
        yield n, label, cfg if isinstance(cfg, dict) else {}


def _iter_mcp_sources() -> Iterator[tuple[str, str, dict[str, Any]]]:
    """(servidor, fuente, config) desde toda la config MCP, en orden de precedencia."""
    cj = _load_json(CLAUDE_JSON)
    yield from _mcp_from(cj.get("mcpServers"), "global ~/.claude.json")
    for ppath, pv in (cj.get("projects") or {}).items():
        if isinstance(pv, dict):
            yield from _mcp_from(pv.get("mcpServers"), f"project:{ppath}")
    yield from _mcp_from(_load_json(SETTINGS).get("mcpServers"), "settings.json")
    yield from _mcp_from(_load_json(REPO_MCP).get("mcpServers"), "repo .mcp.json")


def scan_mcp_servers() -> list[Capability]:
    """Servidores MCP cableados en config (global + project + settings + repo)."""
    found: dict[str, tuple[str, dict[str, Any]]] = {}  # nombre -> (fuente, config)
    for n, src, cfg in _iter_mcp_sources():
        found.setdefault(n, (src, cfg))
    caps: list[Capability] = []
    for n, (src, cfg) in sorted(found.items()):
        caps.append(
            Capability(
                name=n,
                ctype="mcp_server",
                invocation=f"mcp:{n}.*",
                source=f"config:{src}",
                description=f"Servidor MCP cableado en {src}",
                defined_at=str(CLAUDE_JSON) if "claude.json" in src else None,
                extra={"command": cfg.get("command", "")},
            )
        )
    return caps


def scan_aris_mcp_tools() -> list[Capability]:
    """Tools del servidor MCP aris4u, parseadas de @mcp.tool() en mcp_server.py."""
    caps: list[Capability] = []
    try:
        src = MCP_SERVER_SRC.read_text(encoding="utf-8")
    except OSError:
        return caps
    pat = re.compile(
        r"@mcp\.tool\(\)\s*\n\s*def\s+(\w+)\(([^)]*)\)[^:]*:\s*\n"
        r'\s*(?:"""(.*?)""")?',
        re.DOTALL,
    )
    for m in pat.finditer(src):
        name, sig, doc = m.group(1), m.group(2), (m.group(3) or "")
        first = _norm(doc.strip().split("\n")[0]) if doc.strip() else ""
        caps.append(
            Capability(
                name=f"aris4u.{name}",
                ctype="mcp_tool",
                invocation=f"mcp aris4u → {name}",
                source="repo:integrations/mcp_server.py",
                description=first,
                defined_at=str(MCP_SERVER_SRC),
                namespace="aris4u",
                status="wired",
                extra={"signature": _norm(sig)},
            )
        )
    return caps


def scan_hooks() -> list[Capability]:
    """Hooks cableados por evento en ~/.claude/settings.json (automáticos).

    Fusiona la profundidad de ``data/aris4u_hooks_depth.json`` (qué sub-handler
    corre por evento y cuáles bloquean) para no quedarse en el mero conteo.
    """
    depth = _load_json(HOOKS_DEPTH)
    caps: list[Capability] = []
    for event, entries in (_load_json(SETTINGS).get("hooks") or {}).items():
        cmds: list[str] = []
        for entry in entries or []:
            for hook in entry.get("hooks", []) or []:
                cmd = hook.get("command", "")
                if cmd:
                    cmds.append(cmd)
        d = depth.get(event, {})
        handlers = d.get("handlers", [])
        blocking = d.get("blocking", [])
        desc = f"{len(cmds)} hook(s) cableado(s); {len(handlers)} sub-handler(s)"
        if blocking:
            desc += f"; bloqueantes: {', '.join(blocking)}"
        caps.append(
            Capability(
                name=event,
                ctype="hook",
                invocation="automático (no se invoca a mano)",
                source="settings.json",
                description=desc,
                defined_at=str(SETTINGS),
                status="wired",
                extra={"commands": cmds, "handlers": handlers, "blocking": blocking},
            )
        )
    return caps


# --------------------------------------------------------------------------- #
# Reconciliación con el snapshot de runtime + cobertura
# --------------------------------------------------------------------------- #
def load_runtime_snapshot() -> dict[str, Any]:
    """Carga el set autoritativo de capacidades de la sesión (si existe)."""
    return _load_snapshot_or_seed()


def _pick_runtime_set(
    ctype: str,
    rt_skills: set[str],
    rt_agents: set[str],
    rt_mcp: set[str],
) -> set[str]:
    """Devuelve el set de runtime que corresponde a *ctype*.

    Args:
        ctype: Categoría de la capacidad ('skill', 'command', 'agent', 'mcp_server', …).
        rt_skills: Nombres de skills reportados por el runtime.
        rt_agents: Nombres de agentes reportados por el runtime.
        rt_mcp: Nombres de servidores MCP reportados por el runtime.

    Returns:
        El set contra el que cotejar, o un set vacío si ctype no tiene análogo en runtime.
    """
    if ctype in _SKILL_LIKE:
        return rt_skills
    if ctype == "agent":
        return rt_agents
    if ctype == "mcp_server":
        return rt_mcp
    return set()


def _update_disk_cap(
    cap: Capability,
    rt_skills: set[str],
    rt_agents: set[str],
    rt_mcp: set[str],
    has_snapshot: bool,
    matched: dict[str, set[str]],
) -> None:
    """Actualiza *cap.available* y registra la clave en *matched* si el runtime la confirma.

    Args:
        cap: Capacidad de disco a reconciliar.
        rt_skills: Nombres de skills del runtime.
        rt_agents: Nombres de agentes del runtime.
        rt_mcp: Nombres de servidores MCP del runtime.
        has_snapshot: Indica si se cargó un snapshot de runtime.
        matched: Diccionario mutable que acumula qué claves de runtime ya se casaron.
    """
    rset = _pick_runtime_set(cap.ctype, rt_skills, rt_agents, rt_mcp)
    if not rset:
        if not has_snapshot:
            cap.available = None
        return
    hit = next((k for k in cap.runtime_keys() if k in rset), None)
    cap.available = hit is not None if has_snapshot else None
    if hit is not None:
        bucket = "skill" if cap.ctype in _SKILL_LIKE else cap.ctype
        matched[bucket].add(hit)


def reconcile(disk: list[Capability], snap: dict[str, Any]) -> dict[str, Any]:
    """Casa disco contra runtime y añade las capacidades runtime-only.

    Args:
        disk: capacidades enumeradas desde disco/config.
        snap: snapshot de runtime (skills/agents/mcp_servers/builtin_tools).

    Returns:
        Estructura con la lista unificada de capacidades + reporte de cobertura.
    """
    rt_skills = set(snap.get("skills") or [])
    rt_agents = set(snap.get("agents") or [])
    rt_mcp = set(snap.get("mcp_servers") or [])
    has_snapshot = bool(snap)
    matched: dict[str, set[str]] = {"skill": set(), "agent": set(), "mcp_server": set()}

    for cap in disk:
        _update_disk_cap(cap, rt_skills, rt_agents, rt_mcp, has_snapshot, matched)

    runtime_only = _runtime_only(
        {
            "skill": rt_skills - matched["skill"],
            "agent": rt_agents - matched["agent"],
            "mcp_server": rt_mcp - matched["mcp_server"],
            "builtin_tool": set(snap.get("builtin_tools") or []),
        }
    )
    caps = list(disk) + runtime_only
    return {"capabilities": caps, "coverage": _coverage(disk, snap, matched, runtime_only)}


# Plantilla por categoría para las capacidades runtime-only (no respaldadas en disco).
_RUNTIME_ONLY_SPEC: dict[str, dict[str, Any]] = {
    "skill": {
        "invocation": lambda nm: f"/{nm}",
        "source": "builtin/enterprise (runtime-only)",
        "description": "(definición no en disco — built-in de Claude Code o plugin no-cache)",
    },
    "agent": {
        "invocation": lambda nm: f"Agent(subagent_type='{nm}')",
        "source": "builtin (runtime-only)",
        "description": "(agente built-in de Claude Code)",
    },
    "mcp_server": {
        "invocation": lambda nm: f"mcp:{nm}.*",
        "source": "enterprise/account (runtime-only)",
        "description": "(servidor MCP inyectado por la cuenta/empresa, no en config local)",
    },
    "builtin_tool": {
        "invocation": lambda nm: nm,
        "source": "builtin (runtime-only)",
        "description": "(herramienta núcleo de Claude Code)",
    },
}


def _runtime_only(unmatched: dict[str, set[str]]) -> list[Capability]:
    """Construye las capacidades que el runtime expone pero el disco no respalda."""
    out: list[Capability] = []
    for ctype, names in unmatched.items():
        spec = _RUNTIME_ONLY_SPEC[ctype]
        for nm in sorted(names):
            out.append(
                Capability(
                    name=nm,
                    ctype=ctype,
                    invocation=spec["invocation"](nm),
                    source=spec["source"],
                    description=spec["description"],
                    available=True,
                    status="runtime",
                )
            )
    return out


def _coverage(
    disk: list[Capability],
    snap: dict[str, Any],
    matched: dict[str, set[str]],
    runtime_only: list[Capability],
) -> dict[str, Any]:
    """Reporte SUBSET-FULL: N de TOTAL por categoría (disco vs runtime)."""
    by_type_disk: dict[str, int] = {}
    for c in disk:
        by_type_disk[c.ctype] = by_type_disk.get(c.ctype, 0) + 1

    def _cov(rt_key: str, matched_key: str) -> dict[str, int]:
        total = len(snap.get(rt_key) or [])
        backed = len(matched[matched_key])
        return {"runtime_total": total, "disk_backed": backed, "runtime_only": total - backed}

    return {
        "has_runtime_snapshot": bool(snap),
        "snapshot_captured_at": snap.get("captured_at", "(sin snapshot)"),
        "disk_by_type": by_type_disk,
        "disk_total": len(disk),
        "runtime_only_total": len(runtime_only),
        "skills": _cov("skills", "skill"),
        "agents": _cov("agents", "agent"),
        "mcp_servers": _cov("mcp_servers", "mcp_server"),
    }


# --------------------------------------------------------------------------- #
# Ensamblado + render
# --------------------------------------------------------------------------- #
def _enrich_mcp_server_tools(caps: list[Capability], snap: dict[str, Any]) -> None:
    """Adjunta a cada mcp_server su lista de tools (profundidad, del snapshot)."""
    tools_map = snap.get("mcp_tools") or {}
    for c in caps:
        if c.ctype == "mcp_server":
            tools = tools_map.get(c.name)
            if tools:
                c.extra["tools"] = tools
                c.description = f"{c.description} · {len(tools)} tools"


def _runtime_mcp_tools(snap: dict[str, Any]) -> list[Capability]:
    """Tools MCP individuales (granularidad ruteable) de servers != aris4u.

    aris4u ya se cubre en profundidad desde el source (scan_aris_mcp_tools).
    """
    out: list[Capability] = []
    for server, tools in (snap.get("mcp_tools") or {}).items():
        if server == "aris4u":
            continue
        for t in tools:
            out.append(
                Capability(
                    name=f"{server}.{t}",
                    ctype="mcp_tool",
                    invocation=f"mcp {server} → {t}",
                    source=f"mcp:{server} (runtime/cuenta)",
                    description="(tool MCP; esquema completo vía ToolSearch en runtime)",
                    available=True,
                    status="runtime",
                    namespace=server,
                )
            )
    return out


def _enrich_builtin_purpose(caps: list[Capability], snap: dict[str, Any]) -> None:
    """Rellena el propósito de cada builtin_tool (del snapshot)."""
    purpose = snap.get("builtin_tool_purpose") or {}
    for c in caps:
        if c.ctype == "builtin_tool" and purpose.get(c.name):
            c.description = purpose[c.name]


def _command_paths(cmd: str) -> list[str]:
    """Rutas absolutas embebidas en un string de comando (interpreter + script)."""
    return [
        tok
        for tok in cmd.replace('"', " ").replace("'", " ").split()
        if tok.startswith("/")
    ]


def _check_paths(paths: list[str]) -> tuple[str, str]:
    """('live'|'broken', prueba) según existan las rutas dadas."""
    missing = [p for p in paths if not Path(p).exists()]
    if missing:
        return "broken", "falta: " + ", ".join(p.rsplit("/", 1)[-1] for p in missing)
    return "live", f"{len(paths)} ruta(s) presente(s)"


def _hook_liveness(cap: Capability) -> tuple[str, str]:
    """Verifica la salud de una capacidad de tipo hook.

    Construye la lista de rutas de los comandos cableados y delega a _check_paths.

    Args:
        cap: Capacidad de tipo 'hook' a verificar.

    Returns:
        Tuple (estado, prueba) donde estado es 'live' o 'broken'.
    """
    paths: list[str] = []
    for c in cap.extra.get("commands", []):
        paths += _command_paths(c)
    return _check_paths(paths) if paths else ("live", "cableado")


def _mcp_server_liveness(cap: Capability) -> tuple[str, str]:
    """Verifica la salud de una capacidad de tipo mcp_server.

    Los servidores runtime-only (de cuenta/empresa) se marcan como externos.
    Los que tienen binario en disco se verifican con _check_paths.

    Args:
        cap: Capacidad de tipo 'mcp_server' a verificar.

    Returns:
        Tuple (estado, prueba) donde estado es 'live', 'broken' o 'external'.
    """
    if cap.status == "runtime":
        return "external", "disponible esta sesión (cuenta)"
    paths = _command_paths(cap.extra.get("command", "") or "")
    if paths:
        return _check_paths(paths)
    return "external", f"binario externo: {cap.extra.get('command', '?')}"


def _liveness_of(cap: Capability) -> tuple[str, str]:
    """Verifica la salud REAL de una capacidad → (estado, prueba).

    live = verificada presente/funcional · broken = configurada pero su target falta
    · dormant = en disco, no expuesta esta sesión · external = built-in/cuenta.
    """
    if cap.ctype == "hook":
        return _hook_liveness(cap)
    if cap.ctype == "mcp_server":
        return _mcp_server_liveness(cap)
    if cap.available is False:
        return "dormant", "en disco, no expuesta esta sesión"
    if cap.defined_at:
        exists = Path(cap.defined_at).exists()
        return ("live", "archivo presente") if exists else ("broken", "archivo ausente")
    return "external", "disponible en runtime"


def _verify_liveness(caps: list[Capability]) -> None:
    """Anota liveness + prueba en cada capacidad (paso 2 del enrutador)."""
    for c in caps:
        c.liveness, proof = _liveness_of(c)
        c.extra["liveness_proof"] = proof


def collect() -> dict[str, Any]:
    """Reúne todas las fuentes y reconcilia con el runtime → inventario completo."""
    snap = load_runtime_snapshot()
    disk: list[Capability] = []
    disk += scan_user_skills()
    disk += scan_user_agents()
    disk += scan_user_commands()
    disk += scan_plugins()
    disk += scan_mcp_servers()
    disk += scan_aris_mcp_tools()
    disk += scan_hooks()

    rec = reconcile(disk, snap)
    caps = rec["capabilities"]
    caps += _runtime_mcp_tools(snap)
    _enrich_mcp_server_tools(caps, snap)  # incluye los servers runtime-only
    _enrich_builtin_purpose(caps, snap)
    _verify_liveness(caps)  # paso 2: salud real (detecta rotos)

    coverage = rec["coverage"]
    coverage["liveness"] = _liveness_summary(caps)

    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "step": "1-2/5 — inventario + liveness (CAPABILITY_ROUTER_PLAN.md §5.1-5.2)",
        "coverage": coverage,
        "capabilities": [asdict(c) for c in caps],
    }


def _snapshot_skill_names(plugin_caps: list[Capability]) -> set[str]:
    """Agrega en un set todos los nombres de skills + commands del disco y plugins.

    Args:
        plugin_caps: Capacidades de todos los plugins instalados.

    Returns:
        Set de nombres de skills/commands descubiertos, incluyendo las seeds nativas.
    """
    names: set[str] = set()
    names.update(c.name for c in scan_user_skills())
    names.update(c.name for c in scan_user_commands())
    names.update(c.name for c in scan_user_workflows())
    names.update(c.name for c in plugin_caps if c.ctype in _SKILL_LIKE)
    names |= set(NATIVE_HARNESS_SKILLS)
    return names


def _snapshot_agent_names(plugin_caps: list[Capability]) -> set[str]:
    """Agrega en un set todos los nombres de agents del disco, plugins y seeds nativas.

    Args:
        plugin_caps: Capacidades de todos los plugins instalados.

    Returns:
        Set de nombres de agents descubiertos, incluyendo las seeds nativas.
    """
    names: set[str] = set()
    names.update(c.name for c in scan_user_agents())
    names.update(c.name for c in plugin_caps if c.ctype == "agent")
    names |= set(NATIVE_HARNESS_AGENTS)
    return names


def _snapshot_mcp_tools(
    mcp_server_names: set[str],
    aris_tool_caps: list[Capability],
    existing_mcp_tools: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Construye el mapa server → tools para el snapshot.

    Prioridad por server:
      1. ``aris4u``: extraído en vivo del código fuente (siempre fresco).
      2. Server en ``existing_mcp_tools``: se preserva del snapshot previo.
      3. Server en ``NATIVE_MCP_TOOL_SEEDS``: se usa la seed de infraestructura.
      4. Server nuevo sin info: no se incluye en el mapa (tools unknown).

    Args:
        mcp_server_names: Set de nombres de servers descubiertos en el config local.
        aris_tool_caps: Capacidades de tools de aris4u (de scan_aris_mcp_tools).
        existing_mcp_tools: mcp_tools del snapshot previo.

    Returns:
        Mapa { server: [tool, ...] } para el campo mcp_tools del snapshot.
    """
    aris_tools = sorted(
        c.name.split(".", 1)[1] for c in aris_tool_caps if "." in c.name
    )
    result: dict[str, list[str]] = {}
    for server in sorted(mcp_server_names):
        if server == "aris4u":
            result[server] = aris_tools
        elif server in existing_mcp_tools:
            result[server] = existing_mcp_tools[server]
        elif server in NATIVE_MCP_TOOL_SEEDS:
            result[server] = NATIVE_MCP_TOOL_SEEDS[server]
    return result


def build_live_snapshot(ts: str | None = None) -> dict[str, Any]:
    """Regenera el snapshot de capacidades desde disco + seeds nativas del harness.

    Orquesta TODOS los scan_* en vivo (skills, agents, commands, workflows, plugins,
    mcp_servers, aris_mcp_tools), fusiona con NATIVE_HARNESS_SKILLS/AGENTS y
    NATIVE_MCP_TOOL_SEEDS, y escribe el resultado a ``data/capability_runtime_snapshot.json``.

    Idempotente: re-ejecutar sobre el mismo estado de disco produce el mismo output.

    Args:
        ts: Timestamp para captured_at (ej. ``"2026-06-30"``).  Si None, usa la
            fecha UTC actual (permite tests deterministas pasando un valor fijo).

    Returns:
        El diccionario snapshot tal como fue escrito a disco.
    """
    if ts is None:
        ts = datetime.now(UTC).strftime("%Y-%m-%d")

    plugin_caps = scan_plugins()
    existing: dict[str, Any] = _load_snapshot_or_seed()
    mcp_server_names: set[str] = {c.name for c in scan_mcp_servers()}

    snapshot: dict[str, Any] = {
        "captured_at": ts,
        "session_surface": existing.get("session_surface") or "Claude Code CLI",
        "note": (
            "Auto-regenerado por build_live_snapshot() — "
            "skills/agents/MCP desde disco + seeds nativas del harness de Claude. "
            "mcp_tools de servers externos preservados del snapshot previo "
            "(no re-descubribles sin correr los servers). "
            "Ver tools/capability_inventory.py §NATIVE_HARNESS_* y §build_live_snapshot."
        ),
        "skills": sorted(_snapshot_skill_names(plugin_caps)),
        "agents": sorted(_snapshot_agent_names(plugin_caps)),
        "mcp_servers": sorted(mcp_server_names),
        "mcp_tools": _snapshot_mcp_tools(
            mcp_server_names,
            scan_aris_mcp_tools(),
            existing.get("mcp_tools") or {},
        ),
        # builtin_tools y builtin_tool_purpose solo son conocidos en runtime;
        # se preservan del snapshot existente para no perder la info capturada.
        "builtin_tools": existing.get("builtin_tools") or [],
        "builtin_tool_purpose": existing.get("builtin_tool_purpose") or {},
    }

    # Write ATÓMICO (tmp + replace): los hooks vivos leen este snapshot en cada
    # prompt; un write_text directo deja ventanas de JSON truncado que degradan el
    # protocolo a set() (flaky detectado en suite 2026-07-01, Tramo 2 robustez).
    tmp = RUNTIME_SNAPSHOT.with_name(RUNTIME_SNAPSHOT.name + ".tmp")
    tmp.write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(RUNTIME_SNAPSHOT)
    return snapshot


def _liveness_summary(caps: list[Capability]) -> dict[str, Any]:
    """Conteo por estado de liveness + lista de capacidades ROTAS (la señal valiosa)."""
    counts: dict[str, int] = {}
    broken: list[str] = []
    for c in caps:
        counts[c.liveness] = counts.get(c.liveness, 0) + 1
        if c.liveness == "broken":
            broken.append(f"{c.ctype}:{c.name} ({c.extra.get('liveness_proof', '')})")
    return {"counts": counts, "broken": broken}


def _color(code: str, text: str, on: bool) -> str:
    """Envuelve texto en ANSI si los colores están activos."""
    return f"\033[{code}m{text}\033[0m" if on else text


def render(data: dict[str, Any], color: bool) -> str:
    """Panel humano: conteos por tipo + cobertura disco vs runtime."""

    def ok(t: str) -> str:
        return _color("32", t, color)

    def warn(t: str) -> str:
        return _color("33", t, color)

    def dim(t: str) -> str:
        return _color("2", t, color)

    def head(t: str) -> str:
        return _color("1;36", t, color)

    cov = data["coverage"]
    caps = data["capabilities"]
    by_type: dict[str, int] = {}
    for c in caps:
        by_type[c["ctype"]] = by_type.get(c["ctype"], 0) + 1

    L: list[str] = []
    L.append(head("ARIS4U — INVENTARIO DE CAPACIDADES (paso 1-2/5: inventario + liveness)"))
    L.append(dim(f"  generado: {data['generated_at']}"))
    L.append("")
    L.append(f"  {ok('●')} TOTAL capacidades: {len(caps)}")
    for t, n in sorted(by_type.items(), key=lambda kv: -kv[1]):
        L.append(dim(f"      {t:14s} {n}"))
    L.append("")
    if cov["has_runtime_snapshot"]:
        L.append(f"  {ok('●')} COBERTURA runtime (snapshot {cov['snapshot_captured_at']}):")
        for cat in ("skills", "agents", "mcp_servers"):
            c = cov[cat]
            tag = ok if c["runtime_only"] == 0 else warn
            L.append(
                "      "
                + tag(
                    f"{cat:12s} {c['disk_backed']} de {c['runtime_total']} en disco"
                    f"  ·  {c['runtime_only']} runtime-only"
                )
            )
    else:
        L.append(f"  {warn('●')} sin snapshot de runtime → solo cobertura de disco")
    L.append("")
    lv = cov.get("liveness")
    if lv:
        counts = lv["counts"]
        order = " · ".join(f"{k}:{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
        broken_n = len(lv["broken"])
        tag = ok if broken_n == 0 else warn
        L.append(f"  {tag('●')} LIVENESS: {order}")
        for b in lv["broken"]:
            L.append("      " + warn(f"✗ {b}"))
    L.append("")
    L.append(dim(f"  disco: {cov['disk_total']} · runtime-only: {cov['runtime_only_total']}"))
    return "\n".join(L)


def main(argv: list[str]) -> int:
    """Punto de entrada CLI.

    Flags:
        --rebuild   Regenera el snapshot desde disco + seeds nativas y muestra el conteo.
        --json      Salida JSON completa del inventario (sin --rebuild).
        --no-color  Desactiva colores ANSI (sin --rebuild).
    """
    if "--rebuild" in argv:
        snap = build_live_snapshot()
        tools_total = sum(len(v) for v in snap["mcp_tools"].values())
        print(
            f"Snapshot rebuilt ({snap['captured_at']}): "
            f"{len(snap['skills'])} skills · "
            f"{len(snap['agents'])} agents · "
            f"{len(snap['mcp_servers'])} MCP servers · "
            f"{tools_total} MCP tools"
        )
        return 0
    data = collect()
    if "--json" in argv:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        return 0
    print(render(data, color="--no-color" not in argv and sys.stdout.isatty()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
