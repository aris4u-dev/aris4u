"""Capacidades de Claude Code + ARIS4U con audit por valor.

Enumera Skills, Agents, MCP servers y API/endpoints — primero los de Claude, luego los
de ARIS4U — desde sus fuentes de verdad en disco, y a cada uno le adjunta un *audit de
capacidad*: estado (activo/inactivo), uso real (de la telemetría
``logs/v16.1-events.jsonl``), redundancia (duplicado entre fuentes) y un veredicto de
valor (usar / promover / revisar / ocioso). Todo read-only y fail-soft.

Diseño honesto: las skills NO están instrumentadas en la telemetría (no hay evento de
invocación de skill), así que su eje "uso" se reporta como ``None`` ("sin instrumentar")
y su veredicto se basa en estado + redundancia, nunca en un uso inventado.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from . import live_data, translate

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"

# Modelos Claude canónicos (fuente: reglas del entorno ~/.claude/rules + engine/v16/config.py).
# Estático a propósito: no hay manifiesto de modelos en disco (es trabajo futuro del motor).
_CLAUDE_MODELS = [
    ("claude-opus-4-8", "Opus 4.8 — razonamiento top (síntesis, decisión, lo sutil)"),
    ("claude-sonnet-4-6", "Sonnet 4.6 — el grueso del fan-out (review, extracción, búsqueda)"),
    ("claude-haiku-4-5", "Haiku 4.5 — trivial mecánico (clasificación, formato, conteo)"),
    ("claude-fable-5", "Fable 5 — variante (DESACTIVADA en el routing actual)"),
]


# --------------------------------------------------------------------------------------
# Frontmatter / metadata
# --------------------------------------------------------------------------------------

def _frontmatter(path: Path) -> dict:
    """Extrae ``name`` y ``description`` del frontmatter YAML de un .md (parser mínimo).

    No usa PyYAML (puede no estar instalado). Lee solo el bloque entre los primeros dos
    ``---`` y saca las dos claves por regex, soportando valores en una línea.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    block = m.group(1) if m else text[:600]
    lines = block.split("\n")
    out: dict[str, str] = {}
    for key in ("name", "description"):
        for i, ln in enumerate(lines):
            km = re.match(rf"^{key}:\s*(.*)$", ln)
            if not km:
                continue
            val = km.group(1).strip()
            if val in ("|", ">", "|-", ">-", "|+", ">+", ""):
                # Block scalar YAML: el valor son las líneas indentadas siguientes.
                body = []
                for nxt in lines[i + 1:]:
                    if nxt.strip() and not nxt.startswith((" ", "\t")):
                        break
                    body.append(nxt.strip())
                val = " ".join(b for b in body if b)
            out[key] = val.strip("'\"")
            break
    return out


def _short(text: str, n: int = 140) -> str:
    """Trunca a ``n`` chars en límite de palabra, sin saltos de línea."""
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------------------
# Uso real (telemetría)
# --------------------------------------------------------------------------------------

def usage(repo: Path | None = None, *, window: int = 100_000) -> dict:
    """Agrega el uso real desde ``logs/v16.1-events.jsonl`` (ventana de las últimas N líneas).

    Ventana amplia por defecto (~todo el log actual) a propósito: el veredicto "ocioso"
    debe significar "nunca usado", no "no usado en los últimos N eventos". El log es de
    pocos MB → parsearlo entero es aceptable para un panel local.

    Returns:
        ``{mcp_server: Counter, mcp_tool: Counter, agents: Counter, hooks: Counter,
        types: Counter, window: int}``. Counters vacíos si el log no existe (fail-soft).
    """
    repo = repo or live_data.DEFAULT_REPO
    path = repo / live_data._EVENTS
    u: dict[str, Counter] = {k: Counter() for k in
                             ("mcp_server", "mcp_tool", "agents", "hooks", "types")}
    # Última fecha de uso (ISO ts) por clave — indicador de "se sigue usando o se enfrió".
    last: dict[str, dict[str, str]] = {"mcp_server": {}, "agents": {}, "hooks": {}}

    def _stamp(bucket: str, key: str, ts: str) -> None:
        if ts and ts > last[bucket].get(key, ""):
            last[bucket][key] = ts

    if not path.is_file():
        return {**u, "last": last, "window": 0}
    events = live_data.parse_events(live_data.tail_lines(path, window))
    for e in events:
        et = e.get("event") or ""
        ts = e.get("ts", "")
        u["types"][et] += 1
        if e.get("hook"):
            u["hooks"][e["hook"]] += 1
            _stamp("hooks", e["hook"], ts)
        if et == "mcp_call":
            srv, tool = e.get("server", "?"), e.get("tool", "?")
            u["mcp_server"][srv] += 1
            u["mcp_tool"][f"{srv}.{tool}"] += 1
            _stamp("mcp_server", srv, ts)
        if e.get("subagent_type"):
            u["agents"][e["subagent_type"]] += 1
            _stamp("agents", e["subagent_type"], ts)
    return {**u, "last": last, "window": len(events)}


def _uso_label(n: int | None) -> str:
    """Etiqueta cualitativa de uso a partir de un conteo (None = sin instrumentar)."""
    if n is None:
        return "sin datos"
    if n == 0:
        return "nulo"
    if n < 10:
        return "bajo"
    if n < 50:
        return "medio"
    return "alto"


# --------------------------------------------------------------------------------------
# Veredicto de valor
# --------------------------------------------------------------------------------------

_VERDICTS = {
    "usar": "✅ usar",
    "promover": "🟡 promover",
    "revisar": "🟡 revisar",
    "ocioso": "🔴 ocioso",
    "inactivo": "🔴 inactivo",
    "activo": "⚪ activo",
}


def _verdict(*, activo: bool, uso: int | None, redundante: bool) -> str:
    """Deriva el código de veredicto del audit de capacidad.

    - inactivo (no habilitado/cableado) → 'inactivo'.
    - redundante → 'revisar' (override: vale consolidar antes que medir uso).
    - con uso instrumentado: 0 → 'ocioso'; <10 → 'promover'; ≥10 → 'usar'.
    - sin instrumentar (uso None): 'activo' (neutral — no se inventa valor por uso).
    """
    if not activo:
        return "inactivo"
    if redundante:
        return "revisar"
    if uso is None:
        return "activo"
    if uso == 0:
        return "ocioso"
    if uso < 10:
        return "promover"
    return "usar"


def _item(name: str, desc: str, source: str, *, activo: bool, uso: int | None,
          redundante: bool, where: str, last_used: str = "", cuando: str = "",
          extra: dict | None = None) -> dict:
    """Construye un item de capacidad con su audit ya resuelto."""
    code = _verdict(activo=activo, uso=uso, redundante=redundante)
    item = {
        "name": name,
        "desc": _short(desc),
        "source": source,
        "estado": "activo" if activo else "inactivo",
        "uso": uso,
        "uso_label": _uso_label(uso),
        "last_used": last_used,           # ISO ts de la última invocación ("" = nunca/sin datos)
        "last_used_label": _ago(last_used),
        "cuando": cuando or _cuando_from_desc(desc),
        "redundante": redundante,
        "verdict": code,
        "verdict_label": _VERDICTS[code],
        "where": where,
    }
    if extra:
        item.update(extra)
    return item


def _ago(ts: str) -> str:
    """Etiqueta legible de antigüedad a partir de un ISO ts (solo fecha, sin hora)."""
    if not ts:
        return "nunca"
    return ts[:10]  # YYYY-MM-DD — el frontend calcula "hace N días" contra la fecha de hoy


def _cuando_from_desc(desc: str) -> str:
    """Extrae el 'cuándo usar' de una descripción (la frase tras 'Use when/Úsalo/Cuándo')."""
    if not desc:
        return ""
    m = re.search(r"(?:use when|use(?:\s+this)?|úsa\w*|cuando|when to use)\b[:\s]+(.+)",
                  desc, re.I)
    return _short(m.group(1), 120) if m else ""


def _summary(items: list[dict]) -> dict:
    """Conteos por veredicto + total para la cabecera de la sección."""
    c = Counter(i["verdict"] for i in items)
    return {"total": len(items), **{k: c.get(k, 0) for k in _VERDICTS}}


def _grouped(items: list[dict], category: str) -> dict:
    """Empaqueta items en grupos Claude→ARIS4U con summary (orden fijo Claude primero).

    Traduce al español las descripciones de fuente 'claude' (vienen en inglés) usando el
    caché de traducciones; instantáneo (sin llamar a Ollama en el request).
    """
    # Traduce al español TODA descripción que no lo esté ya (los .md del roster ARIS4U también
    # vienen en inglés). translate() devuelve el original si ya es español o si falta en caché.
    cache = translate.load_cache()
    for it in items:
        it["desc"] = translate.translate(it["desc"], cache)
        if it.get("cuando"):
            it["cuando"] = translate.translate(it["cuando"], cache)
    groups = []
    for src, label in (("claude", "Claude"), ("aris4u", "ARIS4U")):
        sub = [i for i in items if i["source"] == src]
        if sub:
            groups.append({"source": src, "label": label, "items": sub,
                           "summary": _summary(sub)})
    return {"available": True, "category": category, "groups": groups,
            "summary": _summary(items)}


def _is_aris(name: str, path_str: str) -> bool:
    """Heurística de procedencia: una capacidad es de ARIS4U si su nombre o ruta lo delatan."""
    n, p = name.lower(), path_str.lower()
    return (n.startswith("aris") or "aris4u" in n or "aris4u" in p
            or n in {"status", "harvest", "preflight", "verify-claims", "multi-research",
                     "mcp-audit", "backup-verify", "skill-security-scan"})


# --------------------------------------------------------------------------------------
# Skills
# --------------------------------------------------------------------------------------

def read_skills(home: Path | None = None) -> dict:
    """Skills de Claude Code (globales + plugins) y de ARIS4U, con audit.

    Fuentes: ``~/.claude/skills/*/SKILL.md`` (globales), los SKILL.md de plugins en cache,
    y ``~/projects/aris4u/skills/*/SKILL.md``. Uso = None (no instrumentadas); redundancia
    = mismo nombre en más de una ubicación.
    """
    home = home or HOME
    # (name, desc, path, origin) — origin identifica la PROCEDENCIA real (no la versión):
    # "global" (~/.claude/skills), "repo" (aris4u/skills) o el plugin concreto.
    found: list[tuple[str, str, Path, str]] = []
    skills_dir = home / ".claude" / "skills"
    if skills_dir.is_dir():
        for d in sorted(skills_dir.iterdir()):
            md = d / "SKILL.md"
            if md.is_file():
                fm = _frontmatter(md)
                found.append((fm.get("name", d.name), fm.get("description", ""), md, "global"))
    found += _plugin_skills(home / ".claude" / "plugins" / "cache")
    repo_skills = home / "projects" / "aris4u" / "skills"
    if repo_skills.is_dir():
        for md in sorted(repo_skills.glob("*/SKILL.md")):
            fm = _frontmatter(md)
            found.append((fm.get("name", md.parent.name), fm.get("description", ""), md, "repo"))

    # Redundancia REAL = el mismo nombre respaldado por más de un archivo FÍSICO distinto.
    # Se resuelve el realpath (``resolve()``) para que un symlink global→repo cuente como UNO
    # solo (no como redundancia); varias versiones de un plugin ya se deduplicaron arriba.
    reals_by_name: dict[str, set[str]] = {}
    for name, _, md, _ in found:
        try:
            reals_by_name.setdefault(name, set()).add(str(md.resolve()))
        except OSError:
            reals_by_name.setdefault(name, set()).add(str(md))

    items, seen = [], set()
    for name, desc, md, _ in found:
        src = "aris4u" if _is_aris(name, str(md)) else "claude"
        key = (name, src)
        if key in seen:
            continue
        seen.add(key)
        items.append(_item(name, desc, src, activo=True, uso=None,
                           redundante=len(reals_by_name[name]) > 1,
                           where=str(md).replace(str(home), "~")))
    items.sort(key=lambda i: (i["source"] != "aris4u", i["name"].lower()))
    return _grouped(items, "skills")


def _plugin_skills(cache: Path) -> list[tuple[str, str, Path, str]]:
    """Skills de plugins en cache, deduplicadas por (plugin, skill): una sola versión.

    El cache guarda varias versiones del mismo plugin (figma/2.2.49, 2.2.60, hashes…). Sin
    deduplicar, una skill aparecería N veces y se marcaría redundante por error. Aquí se
    queda la versión más nueva por (marketplace/plugin, nombre de skill), comparando por
    versión SEMÁNTICA (no lexicográfica: "2.2.60" > "2.2.9", y un hash de dev pierde ante
    un semver).
    """
    if not cache.is_dir():
        return []
    best: dict[tuple[str, str], tuple[tuple, Path]] = {}  # (plugin, skill) -> (verkey, md)
    for md in cache.glob("*/*/*/skills/*/SKILL.md"):
        rel = md.relative_to(cache).parts  # mkt, plugin, version, "skills", skill, "SKILL.md"
        if len(rel) < 6:
            continue
        plugin, version, skill = f"{rel[0]}/{rel[1]}", rel[2], rel[4]
        key = (plugin, skill)
        vk = _version_key(version)
        if key not in best or vk > best[key][0]:
            best[key] = (vk, md)
    out = []
    for (plugin, _), (_, md) in sorted(best.items()):
        fm = _frontmatter(md)
        out.append((fm.get("name", md.parent.name), fm.get("description", ""), md, plugin))
    return out


def _version_key(version: str) -> tuple:
    """Clave de orden semántico: un semver 'X.Y.Z' ordena por enteros y gana a un hash de dev."""
    parts = version.split(".")
    if parts and all(p.isdigit() for p in parts):
        return (1, tuple(int(p) for p in parts))
    return (0, (version,))  # hash/no-semver: prioridad menor que cualquier semver


# --------------------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------------------

def read_agents(home: Path | None = None, repo: Path | None = None) -> dict:
    """Agent types de Claude Code (globales + plugins) y de ARIS4U, con uso real.

    Fuentes: ``~/.claude/agents/*.md`` + agents de plugins en cache. Uso = invocaciones
    de ``subagent_type`` en la telemetría.
    """
    home = home or HOME
    usg = usage(repo)
    u, last = usg["agents"], usg["last"]["agents"]
    found: list[tuple[str, str, Path]] = []
    agents_dir = home / ".claude" / "agents"
    if agents_dir.is_dir():
        for md in sorted(agents_dir.glob("*.md")):
            fm = _frontmatter(md)
            found.append((fm.get("name", md.stem), fm.get("description", ""), md))
    cache = home / ".claude" / "plugins" / "cache"
    if cache.is_dir():
        for md in sorted(cache.glob("*/*/*/agents/*.md")):
            fm = _frontmatter(md)
            found.append((fm.get("name", md.stem), fm.get("description", ""), md))

    items = []
    seen: set[tuple[str, str]] = set()  # dedup: un agente puede estar duplicado en el cache
    for name, desc, md in found:
        where = str(md).replace(str(home), "~")
        src = _agent_source(name, where)
        key = (name, src)
        if key in seen:
            continue
        seen.add(key)
        items.append(_item(name, desc, src, activo=True, uso=u.get(name, 0),
                           redundante=False, where=where, last_used=last.get(name, "")))
    items.sort(key=lambda i: (i["source"] != "aris4u", -(i["uso"] or 0), i["name"].lower()))
    return _grouped(items, "agents")


def _agent_source(name: str, where: str) -> str:
    """Procedencia de un agente: ARIS4U (plugin propio o roster global del usuario) vs Claude.

    - ruta con 'aris4u' → plugin propio (aris4u-ui-pipeline) = ARIS4U.
    - plugin-namespaced (':') o en el cache de plugins de terceros = Claude.
    - el resto (``~/.claude/agents/*.md``) = roster operativo de ARIS4U.
    """
    if "aris4u" in where.lower():
        return "aris4u"
    if ":" in name or "plugins/cache" in where:
        return "claude"
    return "aris4u"


# --------------------------------------------------------------------------------------
# MCP servers
# --------------------------------------------------------------------------------------

def read_mcp(repo: Path | None = None) -> dict:
    """MCP servers cableados (Claude/globales) + el server propio de ARIS4U, con uso real.

    Fuentes: ``~/.claude.json`` (stdio locales) + servers remotos vistos en la telemetría
    (claude.ai OAuth) + las tools de ``integrations/mcp_server.py``. Uso = suma de
    ``mcp_call`` por server en la telemetría.
    """
    repo = repo or live_data.DEFAULT_REPO
    usg = usage(repo)
    u, last = usg["mcp_server"], usg["last"]["mcp_server"]
    items = []
    seen: set[str] = set()

    # Servers cableados localmente (~/.claude.json → mcpServers).
    # Distinguimos dos casos:
    #   • stdio  (command no vacío): el proceso corre local → sus llamadas APARECEN en la telemetría.
    #            uso = conteo real del log (0 si nunca se llamó → "ocioso").
    #   • remote / HTTP-SSE (command vacío, url presente): las llamadas van al endpoint remoto
    #            directamente a través de Anthropic; NUNCA aparecen en v16.1-events.jsonl.
    #            uso = None ("sin datos locales") → verdict "activo" (neutral, no inventado).
    for name, spec in _local_mcp_servers().items():
        seen.add(name)
        cmd = spec.get("command", "")
        args = " ".join(spec.get("args", []) or [])
        url = spec.get("url", "")
        src = "aris4u" if "aris" in name.lower() else "claude"
        is_remote = not cmd.strip()  # sin comando → conector HTTP/OAuth, no stdio
        if is_remote:
            desc = f"conector remoto HTTP · {url}" if url else "conector remoto / OAuth"
            uso_val: int | None = None   # no medible en telemetría local
            kind = "mcp-remote"
        else:
            desc = f"stdio · {cmd} {args}".strip()
            uso_val = u.get(name, 0)     # 0 si no hay llamadas registradas → "ocioso"
            kind = "mcp-stdio"
        items.append(_item(name, desc, src, activo=True,
                           uso=uso_val, redundante=False, where="~/.claude.json",
                           last_used=last.get(name, ""), extra={"kind": kind}))

    # Fuentes 2+3: MCPs aportados por plugins instalados y plugins locales en desarrollo.
    # Reutiliza _mcp_from_plugin_cache/_mcp_from_local_plugins de live_data (misma lógica
    # que _discover_mcps en /config) para que /cap/mcp sea igualmente completo.
    claude_dir = HOME / ".claude"
    plugin_entries = (live_data._mcp_from_plugin_cache(claude_dir)
                      + live_data._mcp_from_local_plugins(claude_dir))
    for entry in plugin_entries:
        name = entry["name"]
        if name in seen:
            continue
        seen.add(name)
        cmd = entry.get("command", "")
        url = entry.get("url", "")
        src = "aris4u" if "aris" in name.lower() else "claude"
        # FIX figma: _mcp_from_file ahora preserva 'remote' y 'url' separados de 'command'.
        # Antes colapsaba url→command, por lo que is_remote = not cmd.strip() daba False para
        # servers HTTP (type=http, sin command) y los clasificaba como mcp-stdio.
        is_remote = entry.get("remote", False) or not cmd.strip()
        if is_remote:
            desc = f"conector remoto HTTP · {url}" if url else "conector remoto / OAuth"
            uso_val: int | None = None
            kind = "mcp-remote"
        else:
            desc = f"stdio · {cmd}".strip()
            uso_val = u.get(name, 0)
            kind = "mcp-stdio"
        items.append(_item(name, desc, src, activo=True,
                           uso=uso_val, redundante=False,
                           where=entry.get("origin", "plugin"),
                           last_used=last.get(name, ""), extra={"kind": kind}))

    _add_aris_mcp(items, seen, repo, u.get("aris4u", 0), last.get("aris4u", ""))
    _add_remote_mcp(items, seen, u, last)
    items.sort(key=lambda i: (i["source"] != "aris4u", -(i["uso"] or 0)))
    return _grouped(items, "mcp")


def _add_aris_mcp(items: list[dict], seen: set[str], repo: Path, uso: int,
                  last_used: str = "") -> None:
    """Asegura que el server MCP propio de ARIS4U aparezca con su conteo de tools."""
    tools = _aris_mcp_tools(repo)
    if not tools:
        return
    desc = _short(f"server propio · {len(tools)} tools: " + ", ".join(tools))
    if "aris4u" in seen:  # ya vino de la config: solo enriquece
        for it in items:
            if it["name"] == "aris4u":
                it["desc"], it["tools"] = desc, tools
        return
    seen.add("aris4u")
    items.append(_item("aris4u", desc, "aris4u", activo=True, uso=uso, redundante=False,
                       where="integrations/mcp_server.py", last_used=last_used,
                       extra={"kind": "mcp-stdio", "tools": tools}))


def _add_remote_mcp(items: list[dict], seen: set[str], u: Counter,
                    last: dict[str, str]) -> None:
    """Añade los servers remotos vistos en la telemetría (claude.ai OAuth) no ya cableados."""
    for srv, n in u.most_common():
        if srv in seen or srv in ("?", ""):
            continue
        seen.add(srv)
        nice = srv.replace("claude_ai_", "").replace("_", " ")
        items.append(_item(nice, "MCP remoto (claude.ai connector)", "claude", activo=True,
                           uso=n, redundante=False, where="claude.ai connectors",
                           last_used=last.get(srv, ""), extra={"kind": "mcp-remote"}))


def _local_mcp_servers() -> dict:
    """Delega a ``live_data._read_global_claude_servers`` — punto único de cambio.

    Antes releia ``~/.claude.json`` por su cuenta; ahora delega a la función canónica
    de live_data para garantizar que /config y /cap/mcp nunca diverjan. No hay ciclo de
    importación: capabilities importa live_data, pero live_data NO importa capabilities.
    """
    return live_data._read_global_claude_servers(HOME)


def _aris_mcp_tools(repo: Path) -> list[str]:
    """Nombres de las tools del server MCP de ARIS4U (grep de def tras @mcp.tool())."""
    src = repo / "integrations" / "mcp_server.py"
    if not src.is_file():
        return []
    text = src.read_text(encoding="utf-8", errors="replace")
    return re.findall(r"@mcp\.tool\(\)\s*\n\s*(?:async\s+)?def\s+(\w+)", text)


# --------------------------------------------------------------------------------------
# API / capacidades
# --------------------------------------------------------------------------------------

def read_api(repo: Path | None = None) -> dict:
    """API y capacidades: modelos de Claude + endpoints/hooks de ARIS4U, con uso real.

    Claude: los modelos disponibles (la cognición rentada). ARIS4U: los endpoints HTTP de
    la consola (su API) agrupados por kind + los hooks cableados (uso real por hook).
    """
    repo = repo or live_data.DEFAULT_REPO
    u = usage(repo)
    items: list[dict] = []

    # Claude — modelos (uso no medible por modelo de forma fiable → sin instrumentar).
    for mid, desc in _CLAUDE_MODELS:
        activo = "DESACTIVADA" not in desc
        items.append(_item(mid, desc, "claude", activo=activo, uso=None, redundante=False,
                           where="rules + engine/v16/config.py", extra={"kind": "model"}))

    # ARIS4U — endpoints de la consola (la API de ARIS4U) agrupados por kind.
    endpoints = _console_endpoints()
    by_kind = Counter(e.get("kind", "?") for e in endpoints)
    for kind, cnt in by_kind.most_common():
        paths = [e["path"] for e in endpoints if e.get("kind") == kind]
        items.append(_item(f"API · {kind}", f"{cnt} endpoints {kind}: " + ", ".join(paths[:8])
                           + ("…" if len(paths) > 8 else ""), "aris4u", activo=True, uso=None,
                           redundante=False, where="console/server.py ENDPOINTS",
                           extra={"kind": "api-endpoints", "count": cnt}))

    # ARIS4U — hooks cableados con uso real (disparos en la ventana de telemetría).
    last_hooks = u["last"]["hooks"]
    for hook, n in u["hooks"].most_common(12):
        items.append(_item(f"hook · {hook}", "Hook cableado de ARIS4U (corre solo por evento)",
                           "aris4u", activo=True, uso=n, redundante=False,
                           where="settings.json hooks", last_used=last_hooks.get(hook, ""),
                           extra={"kind": "hook"}))
    return _grouped(items, "api")


def _console_endpoints() -> list[dict]:
    """Lee la lista ENDPOINTS del server de la consola (import del módulo, fail-soft)."""
    try:
        from . import server
        return [e for e in server.ENDPOINTS if isinstance(e, dict)]
    except Exception:
        return []


# --------------------------------------------------------------------------------------
# Fachada
# --------------------------------------------------------------------------------------

CATEGORIES = ("skills", "agents", "mcp", "api")


def read_capability(category: str, repo: Path | None = None) -> dict:
    """Despacha a la lectora de una categoría ('skills'|'agents'|'mcp'|'api'). Fail-soft."""
    if category not in CATEGORIES:
        return {"available": False, "reason": f"categoría desconocida: {category}"}
    try:
        if category == "skills":
            return read_skills()
        if category == "agents":
            return read_agents(repo=repo)
        if category == "mcp":
            return read_mcp(repo)
        return read_api(repo)
    except Exception as e:  # fail-soft como el resto de lectores de la consola
        return {"available": False, "reason": f"error interno: {str(e)[:120]}"}


# --------------------------------------------------------------------------------------
# Health / smoke test — ¿la capacidad está bien formada y alcanzable?
# --------------------------------------------------------------------------------------

def health(category: str, repo: Path | None = None) -> dict:
    """Smoke test por capacidad: ¿está bien formada y alcanzable? (sin invocarla de verdad).

    - skills/agents: el archivo existe y su frontmatter trae name + description.
    - mcp: el binario del comando existe en PATH (stdio); el server propio expone tools.
    - api: modelos = configurados; endpoints = la ruta existe en el server; hooks = cableados.

    Returns:
        ``{available, category, results:[{name, ok, detail}], summary:{ok, fail}}``.
    """
    repo = repo or live_data.DEFAULT_REPO
    if category not in CATEGORIES:
        return {"available": False, "reason": f"categoría desconocida: {category}"}
    try:
        results = _HEALTH[category](repo)
    except Exception as e:
        return {"available": False, "reason": f"error interno: {str(e)[:120]}"}
    ok = sum(1 for r in results if r["ok"])
    return {"available": True, "category": category, "results": results,
            "summary": {"total": len(results), "ok": ok, "fail": len(results) - ok}}


def _hr(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def _health_md(found: list[tuple[str, str, Path, str]] | list) -> list[dict]:
    """Valida archivos .md de skills/agents: existe + frontmatter con name y description."""
    out, seen = [], set()
    for tup in found:
        name, md = tup[0], tup[2]
        if name in seen:
            continue
        seen.add(name)
        if not md.is_file():
            out.append(_hr(name, False, "archivo no encontrado"))
            continue
        fm = _frontmatter(md)
        if fm.get("name") and fm.get("description"):
            out.append(_hr(name, True, "frontmatter válido (name + description)"))
        else:
            falta = ", ".join(k for k in ("name", "description") if not fm.get(k))
            out.append(_hr(name, False, f"frontmatter incompleto (falta: {falta})"))
    return out


def _health_skills(repo: Path) -> list[dict]:
    home = HOME
    found: list = []
    sd = home / ".claude" / "skills"
    if sd.is_dir():
        found += [(d.name, "", d / "SKILL.md", "") for d in sorted(sd.iterdir())
                  if (d / "SKILL.md").is_file()]
    found += [(t[0], "", t[2], "") for t in _plugin_skills(home / ".claude" / "plugins" / "cache")]
    rs = home / "projects" / "aris4u" / "skills"
    if rs.is_dir():
        found += [(md.parent.name, "", md, "") for md in sorted(rs.glob("*/SKILL.md"))]
    return _health_md(found)


def _health_agents(repo: Path) -> list[dict]:
    home = HOME
    found: list = []
    ad = home / ".claude" / "agents"
    if ad.is_dir():
        found += [(md.stem, "", md, "") for md in sorted(ad.glob("*.md"))]
    cache = home / ".claude" / "plugins" / "cache"
    if cache.is_dir():
        seen = set()
        for md in sorted(cache.glob("*/*/*/agents/*.md")):
            if md.stem not in seen:
                seen.add(md.stem)
                found.append((md.stem, "", md, ""))
    return _health_md(found)


def _codegraph_detail(binpath: str, repo: Path) -> str:
    """Detail label para codegraph: refleja usabilidad real (indexado vs. ocioso/sin índice).

    Fix #3 clase-A (7º gate adversarial 2026-06-29): el binario existe en /opt/homebrew pero
    el workspace no tiene índice ``.codegraph/`` → reportar "sin índice/ocioso" en vez de "ok".
    """
    has_index = (repo / ".codegraph").is_dir()
    suffix = "" if has_index else " · sin índice/ocioso"
    return f"binario 'codegraph' en {binpath}{suffix}"


def _health_mcp_plugin_entries(seen: set[str], repo: Path) -> list[dict]:
    """Items de salud para MCPs de plugin-cache, plugins locales y telemetría (no en ~/.claude.json).

    Fix #2 clase-A (7º gate adversarial 2026-06-29): ``_health_mcp`` solo iteraba
    ``_local_mcp_servers()`` (6 servers), omitiendo los MCPs de plugins (figma, shadcn,
    firebase, serena, mcp-search) y los connectors remotos vistos en telemetría
    (claude-in-chrome, Google Drive, Intuit QuickBooks, ide). Ahora el conjunto testeado
    coincide exactamente con el de ``/cap/mcp``.

    Remotos (remote=True o command vacío) → "remoto (sin binario)" ok=True (no hay binario
    local que verificar). Stdio → shutil.which sobre el comando del plugin.
    """
    import shutil
    out: list[dict] = []
    entries = (
        live_data._mcp_from_plugin_cache(CLAUDE_DIR)
        + live_data._mcp_from_local_plugins(CLAUDE_DIR)
        + live_data._mcp_from_telemetry(repo)
    )
    for entry in entries:
        name = entry["name"]
        if name in seen:
            continue
        seen.add(name)
        cmd = entry.get("command", "")
        url = entry.get("url", "")
        is_remote = entry.get("remote", False) or not cmd.strip()
        if is_remote:
            detail = f"remoto (sin binario) · {url}" if url else "remoto (sin binario)"
            out.append(_hr(name, True, detail))
        else:
            binpath = shutil.which(cmd)
            out.append(_hr(name, bool(binpath),
                           f"binario '{cmd}' en {binpath}" if binpath
                           else f"binario '{cmd}' no encontrado"))
    return out


def _health_mcp(repo: Path) -> list[dict]:
    import shutil
    out: list[dict] = []
    seen: set[str] = set()  # dedup por nombre — aris4u no aparece dos veces
    for name, spec in _local_mcp_servers().items():
        cmd = spec.get("command", "")
        url = spec.get("url", "")
        is_remote = not cmd.strip()
        if is_remote:
            detail = f"sin binario (remoto) · {url}" if url else "sin binario (remoto / OAuth)"
            out.append(_hr(name, True, detail))
        else:
            binpath = shutil.which(cmd)
            # Fix #3: codegraph con binario presente pero sin índice → label honesto.
            if binpath and cmd == "codegraph":
                detail = _codegraph_detail(binpath, repo)
            elif binpath:
                detail = f"binario '{cmd}' en {binpath}"
            else:
                detail = f"binario '{cmd}' no encontrado"
            out.append(_hr(name, bool(binpath), detail))
        seen.add(name)
    # Fix #2: añade MCPs de plugins y telemetría (misma fuente que /cap/mcp).
    out.extend(_health_mcp_plugin_entries(seen, repo))
    # Verifica las tools expuestas del server MCP de ARIS4U.
    # Si 'aris4u' ya está en seen (vino de _local_mcp_servers o plugins), enriquece su detail
    # en vez de añadir una segunda entrada → summary.total = servidores únicos.
    tools = _aris_mcp_tools(repo)
    tool_detail = f"{len(tools)} tools expuestas" if tools else "mcp_server.py no encontrado"
    if "aris4u" in seen:
        for r in out:
            if r["name"] == "aris4u":
                r["detail"] = f"{r['detail']} · {tool_detail}"
                break
    else:
        out.append(_hr("aris4u", bool(tools), tool_detail))
    return out


def _health_api(repo: Path) -> list[dict]:
    out = []
    for mid, desc in _CLAUDE_MODELS:
        activo = "DESACTIVADA" not in desc
        out.append(_hr(mid, activo, "configurado y activo" if activo else "desactivado a propósito"))
    endpoints = _console_endpoints()
    out.append(_hr("API · endpoints", bool(endpoints),
                   f"{len(endpoints)} endpoints declarados en el server"))
    wired = _wired_hooks(repo)
    out.append(_hr("hooks", bool(wired), f"{wired} hooks cableados en settings.json" if wired
                   else "no se pudo leer settings.json"))
    return out


def _wired_hooks(repo: Path) -> int:
    """Cuenta hooks cableados en ~/.claude/settings.json (fail-soft a 0)."""
    import json
    path = HOME / ".claude" / "settings.json"
    if not path.is_file():
        return 0
    try:
        hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
    except (OSError, ValueError):
        return 0
    return sum(len(g.get("hooks", [])) for groups in hooks.values()
               if isinstance(groups, list) for g in groups if isinstance(g, dict))


_HEALTH = {
    "skills": _health_skills,
    "agents": _health_agents,
    "mcp": _health_mcp,
    "api": _health_api,
}
