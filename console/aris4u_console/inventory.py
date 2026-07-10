#!/usr/bin/env python3
"""Generador de inventario VIVO de ARIS4U — el corazón anti-desactualización de la Console.

Descubre los componentes de ARIS4U **leyendo el código vivo** (no una lista a mano: un hook,
guard, tool o MCP-tool nuevo aparece solo), y deriva la señal de cada uno (existe, LOC, tiene
test, último commit, madurez derivada). Reusa el patrón de introspección de
``~/.claude/bin/status-gen.py`` (subprocess + git). Salida: ``out/inventory.json`` + resumen.

Filosofía (handover Live Console §4-6): el inventario técnico (el QUÉ) se genera del código y
NUNCA queda stale; el porqué/producto lo aporta la memoria de ARIS4U (capa posterior). El molde
de lo que debe producir es el HTML curado ``architecture/ARIS4U-REPORTE-COMPLETO.html`` (intocable).

Uso:
    python3 -m aris4u_console.inventory [--repo PATH] [--out PATH] [--json]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone, UTC
from pathlib import Path

DEFAULT_REPO = Path.home() / "projects" / "aris4u"
RECENT_DAYS = 30  # umbral para considerar un componente "tocado recientemente"


def run(cmd: list[str], cwd: Path, timeout: int = 20) -> str:
    """Corre un comando y devuelve stdout (vacío si falla). No lanza excepción."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
        return proc.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


@dataclass
class Component:
    """Un componente descubierto + su señal viva."""

    id: str
    name: str
    family: str
    path: str
    role: str
    maturity: str
    signals: dict = field(default_factory=dict)


# --- señales por componente -----------------------------------------------------------

def _module_docstring(path: Path) -> str:
    """Primera línea del docstring del módulo (rol en lenguaje natural), o ''."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    m = re.search(r'"""(.+?)(?:\n|""")', text, re.S)
    return m.group(1).strip()[:140] if m else ""


def _loc(path: Path) -> int:
    """Líneas del archivo (0 si no se puede leer)."""
    try:
        return sum(1 for _ in path.open("r", encoding="utf-8", errors="ignore"))
    except OSError:
        return 0


def _py_structure(path: Path) -> dict:
    """Estructura expuesta del módulo: funciones y clases (con sus métodos) vía AST.

    Args:
        path: Ruta al archivo .py.

    Returns:
        Dict ``{functions: [...], classes: [{name, methods}], n_functions, n_classes}``.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, SyntaxError, ValueError):
        return {"functions": [], "classes": [], "n_functions": 0, "n_classes": 0}
    funcs: list[str] = []
    classes: list[dict] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(node.name)
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            classes.append({"name": node.name, "methods": methods})
    return {"functions": funcs, "classes": classes,
            "n_functions": len(funcs), "n_classes": len(classes)}


def _last_commit_date(path: Path, repo: Path) -> str:
    """Fecha (YYYY-MM-DD) del último commit que tocó el path, o '' si none."""
    rel = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
    return run(["git", "log", "-1", "--format=%cd", "--date=short", "--", rel], repo)


def _build_tests_meta(repo: Path) -> list[tuple[str, str]]:
    """Lee TODOS los archivos de prueba una vez: lista de (contenido, fecha-último-commit).

    Detectar cobertura por CONTENIDO (no por nombre de archivo) captura las piezas probadas
    dentro de tests de integración (p. ej. phi_guard en test_pre_tool_use.py).
    """
    tdir = repo / "tests"
    if not tdir.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for p in tdir.rglob("*.py"):
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        out.append((content, _last_commit_date(p, repo)))
    return out


def _test_signal(stem: str, tests_meta: list[tuple[str, str]]) -> tuple[bool, str]:
    """Detecta cobertura de un componente por referencia en el contenido de los tests.

    Args:
        stem: Nombre base del componente (>= 4 chars para evitar falsos positivos).
        tests_meta: Lista (contenido, fecha) de _build_tests_meta.

    Returns:
        ``(has_test, last_tested)``: si algún test lo referencia, y la fecha más reciente.
    """
    if len(stem) < 4:
        return (False, "")
    pat = re.compile(r"\b" + re.escape(stem) + r"\b")
    dates = [d for content, d in tests_meta if pat.search(content)]
    if not dates:
        return (False, "")
    return (True, max(d for d in dates if d) if any(dates) else "")


def _derive_maturity(has_test: bool, last_commit: str) -> str:
    """Madurez DERIVADA de señales (honesta: no es una etiqueta pegada a mano)."""
    if not has_test:
        return "sin_test"
    recent = False
    if last_commit:
        try:
            d = datetime.strptime(last_commit, "%Y-%m-%d").date()
            recent = (date.today() - d).days <= RECENT_DAYS
        except ValueError:
            recent = False
    return "vivo" if recent else "estable"


def _enrich(comp: Component, path: Path, repo: Path,
            tests_meta: list[tuple[str, str]]) -> Component:
    """Rellena role/maturity/signals de un componente de archivo .py."""
    has_test, last_tested = _test_signal(path.stem, tests_meta)
    last_commit = _last_commit_date(path, repo)
    comp.role = comp.role or _module_docstring(path)
    comp.signals = {
        "exists": True,
        "loc": _loc(path),
        "has_test": has_test,
        "last_commit": last_commit,
        "last_tested": last_tested,
        "structure": _py_structure(path),
    }
    comp.maturity = _derive_maturity(has_test, last_commit)
    return comp


# --- descubrimiento recursivo ---------------------------------------------------------

_EXCLUDE_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
                 "out", "node_modules", "tests", ".planning", "console"}
# "console": la propia Live Console vive ahora DENTRO del motor (~/projects/aris4u/console).
# Se excluye del escaneo recursivo del motor y se descubre aparte vía discover_console()
# (familia "console") para no contarla dos veces.


def _family_for(rel: str) -> str:
    """Deriva la familia de un .py según su ruta (para el escaneo recursivo)."""
    if rel.startswith("hooks/dispatch/events/"):
        return "hook_event"
    if rel.startswith("hooks/dispatch/handlers/"):
        return "hook_handler"
    if rel.startswith("hooks/"):
        return "hook_core"
    if rel.startswith("engine/v16/orchestration/"):
        return "orchestration"
    if rel.startswith("engine/"):
        return "engine"
    if rel.startswith("integrations/"):
        return "integration"
    if rel.startswith("docs/"):
        return "artifact"
    return "tool"  # tools/, architecture/, evals/, raíz, etc.


def discover_all_py(repo: Path, tests_meta: list[tuple[str, str]]) -> list[Component]:
    """Descubre RECURSIVAMENTE todos los .py del repo (excl venv/git/tests/.planning).

    Reemplaza el escaneo de carpetas fijas → cobertura 100% (incluye dispatch.py, subpaquetes
    como tools/stack_registry, evals/, scripts en docs/, y cualquier archivo futuro).
    """
    out: list[Component] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS and not d.startswith(".venv")]
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            p = Path(dirpath) / fn
            rel = str(p.relative_to(repo))
            name = p.stem if p.stem != "__init__" else f"{p.parent.name}/__init__"
            c = Component(id=f"py:{rel}", name=name, family=_family_for(rel), path=rel,
                          role="", maturity="")
            out.append(_enrich(c, p, repo, tests_meta))
    return sorted(out, key=lambda c: c.path)


def discover_mcp_tools(repo: Path) -> list[Component]:
    """Descubre las MCP tools (funciones decoradas con @mcp.tool en mcp_server.py)."""
    server = repo / "integrations" / "mcp_server.py"
    if not server.is_file():
        return []
    text = server.read_text(encoding="utf-8", errors="ignore")
    names = re.findall(r"@mcp\.tool\(\)\s*\n\s*def\s+(\w+)", text)
    rel = str(server.relative_to(repo))
    return [
        Component(id=f"mcp_tool:{n}", name=n, family="mcp_tool", path=rel,
                  role="MCP tool (opt-in, invocada por Claude)", maturity="vivo",
                  signals={"exists": True})
        for n in names
    ]


def discover_databases(repo: Path) -> list[Component]:
    """Descubre las bases de datos locales (data/*.db) con su tamaño."""
    out: list[Component] = []
    for p in sorted((repo / "data").glob("*.db")) if (repo / "data").is_dir() else []:
        size_mb = round(p.stat().st_size / 1_048_576, 1)
        out.append(Component(
            id=f"db:{p.stem}", name=p.name, family="database",
            path=str(p.relative_to(repo)), role="Base de datos local (memoria/telemetría)",
            maturity="vivo", signals={"exists": True, "size_mb": size_mb},
        ))
    return out


def _simple_component(path: Path, repo: Path, family: str, role: str) -> Component:
    """Componente para un archivo/carpeta NO-código (script, config, artefacto, db)."""
    rel = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
    sig: dict = {"exists": path.exists(), "last_commit": _last_commit_date(path, repo)}
    if path.is_file():
        sig["loc"] = _loc(path)
        size = path.stat().st_size
        if path.suffix == ".db" or size > 100_000:
            sig["size_mb"] = round(size / 1_048_576, 1)
    elif path.is_dir():
        sig["files"] = sum(1 for p in path.rglob("*") if p.is_file())
    return Component(id=f"{family}:{path.name}", name=path.name, family=family,
                     path=rel, role=role, maturity="vivo", signals=sig)


# Los .sh VIVOS en hooks/ (el resto fue portado a dispatch.py y está muerto — ver CLAUDE.md).
_LIVE_HOOK_SH = {"write_client_bridge.sh", "async_vacuum.sh", "nightly_vacuum.sh"}


def discover_scripts(repo: Path) -> list[Component]:
    """Descubre RECURSIVAMENTE todos los .sh; marca muertos los de hooks/ portados a dispatch.py."""
    out: list[Component] = []
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS and not d.startswith(".venv")]
        for fn in sorted(filenames):
            if not fn.endswith(".sh"):
                continue
            p = Path(dirpath) / fn
            c = _simple_component(p, repo, "script", "Script de shell (hook/wrapper/operación)")
            if p.parent.name == "hooks" and fn not in _LIVE_HOOK_SH:
                c.maturity = "muerto"
                c.role = "Script .sh PORTADO a dispatch.py (muerto, queda en disco)"
            out.append(c)
    return sorted(out, key=lambda c: c.path)


_CONFIG_FILES = (".claude-plugin/plugin.json", ".mcp.json", "hooks/hooks.json", "pyproject.toml")


def discover_configs(repo: Path) -> list[Component]:
    """Descubre manifiestos/configuración (plugin.json, .mcp.json, hooks.json, pyproject)."""
    out: list[Component] = []
    for rel in _CONFIG_FILES:
        p = repo / rel
        if p.is_file():
            out.append(_simple_component(p, repo, "config", "Manifiesto / configuración del plugin"))
    return out


_ARTIFACT_DIRS = ("docs", "docs/v17-cvp", "templates", "evals", ".planning")


def discover_artifacts(repo: Path) -> list[Component]:
    """Descubre carpetas-artefacto (docs, plantillas, datasets, corpus) + skills + skills/."""
    out: list[Component] = []
    for rel in _ARTIFACT_DIRS:
        p = repo / rel
        if p.is_dir():
            out.append(_simple_component(p, repo, "artifact", "Carpeta de artefactos/datos/documentos"))
    sk = repo / "skills"
    if sk.is_dir():
        # El contenedor skills/ es un ARTEFACTO (carpeta), no un skill — contarlo como
        # "skill" inflaba el conteo con un fantasma (skill:skills). Los skills reales son sus hijos.
        out.append(_simple_component(sk, repo, "artifact", "Carpeta de skills de ARIS4U"))
        for d in sorted(p for p in sk.iterdir() if p.is_dir()):
            out.append(_simple_component(d, repo, "skill", "Skill de ARIS4U (comando /aris-*)"))
    return out


def discover_external(home: Path) -> list[Component]:
    """Descubre dependencias EXTERNAS al repo: claude-mem.db + settings.json global."""
    out: list[Component] = []
    db = home / ".claude-mem" / "claude-mem.db"
    if db.is_file():
        out.append(Component(
            id="db:claude-mem", name="claude-mem.db", family="database", path=str(db),
            role="Memoria narrativa EXTERNA (plugin claude-mem de terceros, read-only)",
            maturity="vivo",
            signals={"exists": True, "size_mb": round(db.stat().st_size / 1_048_576, 1),
                     "external": True}))
    settings = home / ".claude" / "settings.json"
    if settings.is_file():
        out.append(Component(
            id="config:settings.json", name="settings.json", family="config", path=str(settings),
            role="Cableado global de hooks (instalación viva, ~/.claude/settings.json)",
            maturity="vivo", signals={"exists": True, "external": True}))
    settings_local = home / ".claude" / "settings.local.json"
    if settings_local.is_file():
        out.append(Component(
            id="config:settings.local.json", name="settings.local.json", family="config",
            path=str(settings_local),
            role="Overrides locales de la instalación (~/.claude/settings.local.json)",
            maturity="vivo", signals={"exists": True, "external": True}))
    return out


def _external(c: Component) -> Component:
    """Marca un componente como externo al repo del motor (vive en el wrapper)."""
    c.signals["external"] = True
    return c


def _wrapper_skills(base: Path, exclude: set[str]) -> list[Component]:
    """Skills del wrapper en ``~/.claude/skills`` — TODAS, no solo ``aris-*``.

    Las ``aris-*`` (aris-config, aris-status, aris-council…) son propias de ARIS4U; las demás
    (status, preflight, second-auditor, harvest, pdf, xlsx, multi-research…) son skills de Claude
    Code que el amplificador usa a diario. El mapa "100% real" del sistema debe incluirlas todas;
    filtrar por ``startswith("aris")`` dejaba ciegas ~19 de 24.
    """
    sk = base / "skills"
    if not sk.is_dir():
        return []
    dirs = sorted(p for p in sk.iterdir() if p.is_dir() and p.name not in exclude)
    out: list[Component] = []
    for d in dirs:
        role = (f"Skill del wrapper ARIS4U (comando /{d.name})" if d.name.startswith("aris")
                else f"Skill de Claude Code que el amplificador usa (/{d.name})")
        out.append(_external(_simple_component(d, base, "skill", role)))
    return out


def _wrapper_hooks(base: Path) -> list[Component]:
    """Hooks/guards globales de ``~/.claude/hooks`` — ``.sh`` (incl. la statusline) **y ``.py``**.

    Los hooks Python globales (p.ej. ``static-analysis-gate.py``, ``enterprise-build-hint.py``) son
    gates vivos cableados en ``settings.json``; descubrir solo ``*.sh`` los dejaba invisibles.
    """
    hk = base / "hooks"
    if not hk.is_dir():
        return []
    out: list[Component] = []
    for p in sorted(list(hk.glob("*.sh")) + list(hk.glob("*.py"))):
        if p.name == "statusline.sh":
            role = "Statusline del M5 (barra de estado de ~/.claude/settings.json)"
        elif p.suffix == ".py":
            role = "Hook/guard global Python cableado en ~/.claude/settings.json"
        else:
            role = "Hook/guard global cableado en ~/.claude/settings.json"
        out.append(_external(_simple_component(p, base, "hook_global", role)))
    return out


def _wrapper_bin(base: Path) -> list[Component]:
    """Scripts operativos de ``~/.claude/bin`` (status-gen.py, ram-report.sh, verify-config.sh…)."""
    bn = base / "bin"
    if not bn.is_dir():
        return []
    return [_external(_simple_component(p, base, "bin", "Script operativo del wrapper (~/.claude/bin)"))
            for p in sorted(bn.iterdir()) if p.is_file()]


def _wrapper_workflows(base: Path) -> list[Component]:
    """Workflows de orquestación del wrapper en ``~/.claude/workflows`` (``.js``).

    Scripts de fan-out determinista (agent-benchmark, enterprise-build) que la sesión invoca con
    la herramienta Workflow; ningún descubridor los cubría.
    """
    wf = base / "workflows"
    if not wf.is_dir():
        return []
    return [_external(_simple_component(p, base, "orchestration",
                                        f"Workflow de orquestación del wrapper (/{p.stem})"))
            for p in sorted(wf.glob("*.js"))]


def discover_wrapper(home: Path, exclude_skills: set[str] | None = None) -> list[Component]:
    """Descubre el WRAPPER de ARIS4U en ``~/.claude`` — el amplificador no vive solo en el repo
    del motor: sus skills ``aris-*``, sus hooks/guards globales ``.sh``, los scripts de ``bin/`` y
    la statusline son partes vivas que el mapa "100% real" debe incluir.

    Args:
        home: HOME donde cuelga ``.claude`` (los tests lo apuntan a un tmp para aislarse).
        exclude_skills: Nombres de skills ya descubiertos en el repo (evita doble conteo de las
            que existen tanto en ``repo/skills`` como en ``~/.claude/skills``).

    Returns:
        Componentes del wrapper, todos marcados ``signals.external=True``.
    """
    base = home / ".claude"
    exclude = exclude_skills or set()
    return (_wrapper_skills(base, exclude) + _wrapper_hooks(base)
            + _wrapper_bin(base) + _wrapper_workflows(base))


def discover_console(console_repo: Path) -> list[Component]:
    """Descubre la propia Live Console como parte del mapa (auto-referencia: el órgano que
    observa ARIS4U también es ARIS4U). Escanea ``aris4u_console/*.py`` con su señal viva."""
    pkg = console_repo / "aris4u_console"
    if not pkg.is_dir():
        return []
    tm = _build_tests_meta(console_repo)
    out: list[Component] = []
    for p in sorted(pkg.glob("*.py")):
        rel = str(p.relative_to(console_repo))
        name = p.stem if p.stem != "__init__" else f"{p.parent.name}/__init__"
        c = Component(id=f"console:{rel}", name=name, family="console", path=rel,
                      role="", maturity="")
        out.append(_enrich(c, p, console_repo, tm))
    return out


def build_inventory(repo: Path, external_home: Path | None = None,
                    console_repo: Path | None = None) -> dict:
    """Construye el inventario vivo completo descubriendo todas las familias.

    Args:
        repo: Raíz del repo de ARIS4U (el motor).
        external_home: HOME para dependencias externas + wrapper (claude-mem.db, settings.json,
            skills/hooks/bin de ~/.claude). Default Path.home(); los tests lo apuntan a un tmp.
        console_repo: Raíz del repo de la Live Console para auto-incluirla en el mapa. ``None``
            (default) = no auto-referenciar (los tests no la pasan → conteos aislados).

    Returns:
        Dict con metadata + lista de componentes + conteos por familia/madurez.
    """
    home = external_home if external_home is not None else Path.home()
    tm = _build_tests_meta(repo)
    comps: list[Component] = []
    comps += discover_all_py(repo, tm)          # recursivo: 100% de los .py del motor
    comps += discover_mcp_tools(repo)
    comps += discover_databases(repo)
    comps += discover_external(home)
    comps += discover_scripts(repo)
    comps += discover_configs(repo)
    comps += discover_artifacts(repo)
    repo_skill_names = {c.name for c in comps if c.family == "skill"}
    comps += discover_wrapper(home, exclude_skills=repo_skill_names)  # ~/.claude (motor+wrapper)
    if console_repo is not None:
        comps += discover_console(console_repo)  # la propia consola (auto-referencia)

    by_family: dict[str, int] = {}
    by_maturity: dict[str, int] = {}
    for c in comps:
        by_family[c.family] = by_family.get(c.family, 0) + 1
        by_maturity[c.maturity] = by_maturity.get(c.maturity, 0) + 1

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
    head = run(["git", "rev-parse", "--short", "HEAD"], repo)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "repo": str(repo),
        "git": {"branch": branch, "head": head},
        "totals": {"components": len(comps), "with_tests":
                   sum(1 for c in comps if c.signals.get("has_test"))},
        "by_family": by_family,
        "by_maturity": by_maturity,
        "components": [asdict(c) for c in comps],
    }


_MATURITY_COLOR = {
    "vivo": "#1a7f37", "estable": "#1f6feb", "sin_test": "#9a6700", "desconocido": "#6e7781",
}
_FAMILY_LABEL = {
    "hook_event": "Hooks (eventos)", "hook_handler": "Hooks (guards/handlers)",
    "tool": "Herramientas", "engine": "Motor (engine/v16)", "mcp_tool": "MCP tools",
    "database": "Bases de datos", "hook_global": "Hooks globales (wrapper ~/.claude)",
    "bin": "Scripts del wrapper (~/.claude/bin)", "skill": "Skills (/aris-* + Claude Code)",
    "console": "Live Console (auto-referencia)", "artifact": "Artefactos/datos",
    "script": "Scripts .sh", "config": "Configuración", "orchestration": "Orquestación",
    "hook_core": "Hooks (núcleo)", "integration": "Integraciones (MCP)",
}


def _render_rows(comps: list[dict]) -> str:
    """Filas <tr> de una familia de componentes."""
    rows = []
    for c in comps:
        s = c.get("signals", {})
        mat = c.get("maturity", "desconocido")
        color = _MATURITY_COLOR.get(mat, "#6e7781")
        test = "✓ test" if s.get("has_test") else "— sin test"
        extra = f"{s.get('loc', 0)} líneas · {test} · {s.get('last_commit', '?') or '?'}"
        if "size_mb" in s:
            extra = f"{s['size_mb']} MB · {extra}"
        rows.append(
            f'<tr><td class="nm">{c["name"]}</td>'
            f'<td><span class="badge" style="background:{color}">{mat}</span></td>'
            f'<td class="role">{c.get("role", "") or "—"}</td>'
            f'<td class="sig">{extra}</td></tr>'
        )
    return "\n".join(rows)


def render_html(inv: dict) -> str:
    """Renderiza el inventario a una pantalla HTML autocontenida, de alto contraste."""
    by_fam: dict[str, list[dict]] = {}
    for c in inv["components"]:
        by_fam.setdefault(c["family"], []).append(c)
    sections = []
    for fam in sorted(by_fam, key=lambda f: -len(by_fam[f])):
        label = _FAMILY_LABEL.get(fam, fam)
        sections.append(
            f'<h2>{label} <small>({len(by_fam[fam])})</small></h2>'
            f'<table><thead><tr><th>Componente</th><th>Estado</th><th>Qué hace</th>'
            f'<th>Señal viva</th></tr></thead><tbody>{_render_rows(by_fam[fam])}</tbody></table>'
        )
    t = inv["totals"]
    g = inv["git"]
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<title>ARIS4U — Inventario Vivo</title><style>
:root{{color-scheme:light}}
body{{font:16px/1.5 -apple-system,system-ui,sans-serif;color:#1c2024;background:#fbfaf8;
margin:0;padding:2rem;max-width:1100px;margin:auto}}
h1{{font-size:1.6rem;margin:0 0 .2rem}} h2{{font-size:1.15rem;margin:1.8rem 0 .5rem;
border-bottom:2px solid #e3e0db;padding-bottom:.3rem}} h2 small{{color:#6e7781;font-weight:400}}
.meta{{color:#57606a;font-size:.95rem;margin-bottom:1rem}}
.cards{{display:flex;gap:1rem;flex-wrap:wrap;margin:1rem 0}}
.card{{background:#fff;border:1px solid #e3e0db;border-radius:10px;padding:.8rem 1.1rem}}
.card b{{font-size:1.5rem;display:block}}
table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e0db;
border-radius:8px;overflow:hidden}} th,td{{text-align:left;padding:.55rem .8rem;
border-bottom:1px solid #efece7;vertical-align:top}} th{{background:#f4f2ee;font-size:.9rem}}
.nm{{font-family:ui-monospace,monospace;font-weight:600;white-space:nowrap}}
.role{{color:#39424b;font-size:.92rem}} .sig{{color:#6e7781;font-size:.85rem;white-space:nowrap}}
.badge{{color:#fff;padding:.1rem .5rem;border-radius:20px;font-size:.78rem;font-weight:600}}
</style></head><body>
<h1>🧠 ARIS4U — Inventario Vivo</h1>
<div class="meta">Generado del código: {inv['generated_at'][:19]} ·
rama <b>{g['branch']}</b>@{g['head']} · esta pantalla se regenera sola (nunca queda vieja)</div>
<div class="cards">
<div class="card"><b>{t['components']}</b>componentes</div>
<div class="card"><b>{t['with_tests']}</b>con prueba</div>
<div class="card"><b>{len(by_fam)}</b>familias</div>
</div>
{''.join(sections)}
</body></html>"""


def _print_summary(inv: dict) -> None:
    """Imprime un resumen legible del inventario."""
    print(f"\n=== INVENTARIO VIVO DE ARIS4U · {inv['git']['branch']}@{inv['git']['head']} ===")
    print(f"generado: {inv['generated_at'][:19]}  ·  {inv['totals']['components']} componentes "
          f"·  {inv['totals']['with_tests']} con test")
    print("\n-- por familia --")
    for fam, n in sorted(inv["by_family"].items(), key=lambda x: -x[1]):
        print(f"  {fam:16s}: {n}")
    print("\n-- por madurez (derivada de señales) --")
    for mat, n in sorted(inv["by_maturity"].items(), key=lambda x: -x[1]):
        print(f"  {mat:16s}: {n}")


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada CLI."""
    ap = argparse.ArgumentParser(description="Generador de inventario vivo de ARIS4U")
    console_root = Path(__file__).resolve().parent.parent
    ap.add_argument("--repo", type=Path, default=DEFAULT_REPO, help="raíz del repo ARIS4U")
    ap.add_argument("--out", type=Path, default=console_root / "out" / "inventory.json")
    ap.add_argument("--json", action="store_true", help="volcar el JSON completo a stdout")
    ap.add_argument("--no-console", action="store_true",
                    help="no auto-incluir la propia Live Console en el mapa")
    args = ap.parse_args(argv)

    inv = build_inventory(args.repo, console_repo=None if args.no_console else console_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = args.out.with_suffix(".html")
    html_path.write_text(render_html(inv), encoding="utf-8")
    if args.json:
        print(json.dumps(inv, ensure_ascii=False, indent=2))
    else:
        _print_summary(inv)
        print(f"\nInventario escrito en {args.out}")
        print(f"Pantalla HTML en   {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
