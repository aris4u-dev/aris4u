#!/usr/bin/env python3
"""Renderiza la pantalla SEMÁNTICA de la Live Console (organización humana + drill-down).

Marida DOS capas:
  - capa HUMANA/curada (``data/curated_semantics.json``, extraída del HTML curado del usuario):
    qué es ARIS4U, menú de 8 secciones, inventario por grupos con "para qué sirve" en lenguaje
    sencillo, y los escenarios de comportamiento ("cuándo/dónde funciona").
  - capa VIVA/técnica (``out/inventory.json``, generada del código por ``inventory.py``):
    estado/madurez, LOC, pruebas, último commit y la ESTRUCTURA expuesta (funciones/clases).

Resultado: ``out/console.html`` — pantalla autocontenida, alto contraste, con menú navegable y
componentes que se DESPLIEGAN al hacer clic (para-qué → detalle técnico → estructura → estado vivo).
La capa humana se toma del HTML curado (NO se reinventa); la viva mata el drift.

Uso:
    python3 -m aris4u_console.render_console
"""
from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent
_LIVE = _HERE / "out" / "inventory.json"
_CURATED = _HERE / "data" / "curated_semantics.json"
_OUT = _HERE / "out" / "console.html"

_MAT_COLOR = {"vivo": "#1a7f37", "estable": "#1f6feb", "sin_test": "#9a6700", "": "#6e7781"}


def esc(s: object) -> str:
    """Escapa HTML de cualquier valor."""
    return html.escape(str(s if s is not None else ""))


# --- marriage capa viva ↔ capa curada -------------------------------------------------

_FILE_RE = re.compile(r"[\w./-]+\.(?:py|sh|json|db|toml|md|ya?ml)")


def _live_index(live: dict) -> dict:
    """Índices del inventario vivo para casar: por stem, por ruta y por basename."""
    comps = live.get("components", [])
    by_base: dict[str, list[dict]] = {}
    for c in comps:
        base = (c.get("path", "") or c.get("name", "")).rsplit("/", 1)[-1]
        by_base.setdefault(base, []).append(c)
    return {
        "by_stem": {c["name"]: c for c in comps},
        "by_path": {c["path"]: c for c in comps if c.get("path")},
        "by_base": by_base,
    }


def _match_by_path(name: str, lidx: dict) -> dict | None:
    """Casa por ruta de archivo extraída del nombre: sufijo dir-alineado, o basename."""
    for p in _FILE_RE.findall(name):
        if "/" in p:
            for lp, lc in lidx["by_path"].items():
                if lp == p or lp.endswith("/" + p):
                    return lc
        cands = lidx["by_base"].get(p.rsplit("/", 1)[-1])
        if cands:
            return cands[0]
    return None


def _match_by_token(name: str, lidx: dict) -> dict | None:
    """Casa por identificador exacto (cualquier token \\w{4,} que sea un stem vivo)."""
    for t in re.findall(r"[a-z_][a-z0-9_]{3,}", name.lower()):
        if t in lidx["by_stem"]:
            return lidx["by_stem"][t]
    return None


def _match_by_stem(name: str, lidx: dict) -> dict | None:
    """Fallback fuzzy: primer segmento del nombre como substring de un stem vivo."""
    n = re.sub(r"\(.*?\)", " ", name.lower())
    key = re.split(r"[ /+,—·:]", n.strip())[0].replace(".py", "").strip()
    if not key:
        return None
    for stem, comp in lidx["by_stem"].items():
        if stem == key or (len(key) >= 4 and (key in stem or stem in key)):
            return comp
    return None


def match_live(comp: object, lidx: dict) -> dict | None:
    """Casa un componente curado con su componente vivo (ruta → token → fuzzy)."""
    name = comp.get("name", "") if isinstance(comp, dict) else str(comp)
    return (_match_by_path(name, lidx) or _match_by_token(name, lidx)
            or _match_by_stem(name, lidx))


# --- render de cada sección -----------------------------------------------------------

def _render_que_es(identity: dict) -> str:
    """Sección Inicio: qué es ARIS4U (prueba de 'empezar de cero') + por qué importa."""
    expl = esc(identity.get("fresh_start_explainer", ""))
    full = esc(identity.get("what_is_aris4u", ""))
    why = "".join(f"<li>{esc(w)}</li>" for w in identity.get("why_it_matters", []))
    return f"""<section id="inicio" class="sec active">
<h2>¿Qué es ARIS4U?</h2>
<div class="lead">{expl}</div>
<details class="more"><summary>Ver explicación completa + por qué importa</summary>
<div class="lead" style="border-left-color:#1f6feb;margin-top:.8rem">{full}</div>
<h3>Por qué importa</h3><ul class="why">{why}</ul></details>
</section>"""


def _render_comportamiento(behavior: dict) -> str:
    """Sección Comportamiento: los escenarios = 'dónde/cuándo funciona' (eje humano)."""
    cards = []
    for s in behavior.get("scenarios", []):
        steps = "".join(f"<li>{esc(x)}</li>" for x in s.get("steps", []))
        comps = " · ".join(esc(c) for c in s.get("components_involved", []))
        cards.append(f"""<div class="card">
<h3>{esc(s.get('title'))}</h3>
<div class="when">📍 {esc(s.get('when_it_happens'))}</div>
<ol class="steps">{steps}</ol>
{f'<div class="involved">Piezas: {comps}</div>' if comps else ''}</div>""")
    return f"""<section id="flow" class="sec">
<h2>Comportamiento — dónde y cuándo funciona</h2>
<p class="hint">Cómo actúa ARIS4U en cada momento, paso a paso.</p>
<div class="cards">{''.join(cards)}</div></section>"""


def _struct_lines(st: dict) -> list[str]:
    """Líneas HTML de clases + funciones de una estructura."""
    parts: list[str] = []
    for c in st.get("classes", []):
        meth = ", ".join(esc(m) for m in c.get("methods", [])[:12])
        parts.append(f"<div class='cls'>🧩 clase <code>{esc(c['name'])}</code>"
                     f"<span class='meth'>{meth}</span></div>")
    funcs = st.get("functions", [])
    if funcs:
        fl = ", ".join(f"<code>{esc(f)}</code>" for f in funcs[:20])
        parts.append(f"<div class='fns'>ƒ funciones: {fl}</div>")
    return parts


def _render_structure(live: dict | None) -> str:
    """HTML de la estructura expuesta (funciones/clases) de un componente vivo."""
    if not live:
        return ""
    st = (live.get("signals") or {}).get("structure") or {}
    if not st.get("functions") and not st.get("classes"):
        return ""
    return "<div class='struct'><b>Estructura expuesta:</b>" + "".join(_struct_lines(st)) + "</div>"


# --- meta visual por familia ----------------------------------------------------------
_FAMILY_META = {
    "hook_event": ("🔌", "#8957e5"), "hook_handler": ("🛡️", "#bf8700"),
    "hook_core": ("🧩", "#6e40c9"), "tool": ("🔧", "#1f6feb"), "engine": ("⚙️", "#cf222e"),
    "orchestration": ("📐", "#bc4c00"), "mcp_tool": ("🔭", "#1a7f37"),
    "integration": ("🔗", "#0969da"), "database": ("🗄️", "#57606a"), "script": ("📜", "#6e7781"),
    "config": ("⚙", "#656d76"), "skill": ("✨", "#8250df"), "artifact": ("📦", "#6e7781"),
}
_MAT_LABEL = {"vivo": "vivo", "estable": "estable", "sin_test": "sin prueba",
              "muerto": "muerto", "curado": "curado", "": "—"}


def _scope_badge(comp: dict) -> str:
    """Badge de alcance: marca las piezas del vertical médico (PHI = algunos clientes)."""
    blob = f"{comp.get('name','')} {comp.get('what_for','')} {comp.get('tech_detail','')}".lower()
    if "phi" in blob or "clínic" in blob or "clinic" in blob or "healthcare" in blob:
        return '<span class="chip med">🏥 vertical médico</span>'
    return ""


def _first_sentence(text: str) -> str:
    """Primera oración (el enunciado claro) de un texto."""
    if not text:
        return ""
    return re.split(r"(?<=[.;])\s+", text.strip(), maxsplit=1)[0].strip()


def _to_bullets(text: str) -> list[str]:
    """Parte un párrafo en bullets cortos (oraciones), sin fragmentos triviales."""
    if not text:
        return []
    return [p.strip().rstrip(".") for p in re.split(r"(?<=[.;])\s+", text.strip())
            if len(p.strip()) > 14]


def _sci_content(comp: dict, live: dict | None) -> str:
    """Contenido del POP-UP científico: detalle técnico íntegro + evidencia + estructura + señal."""
    parts = [f"<h3>🔬 {esc(comp.get('name'))}</h3>"]
    tech_b = _to_bullets(comp.get("tech_detail", ""))
    if tech_b:
        parts.append("<h4>Explicación técnica completa</h4><ul>"
                     + "".join(f"<li>{esc(b)}</li>" for b in tech_b) + "</ul>")
    if comp.get("evidence"):
        parts.append(f"<p><b>Evidencia:</b> <code>{esc(comp.get('evidence'))}</code></p>")
    if comp.get("status"):
        parts.append(f"<p><b>Estado (curado):</b> {esc(comp.get('status'))}</p>")
    if live:
        parts.append(_sci_live_block(live))
    return "".join(parts)


def _sci_live_block(live: dict) -> str:
    """Bloque del modal con la señal viva + botón 'ver código' + estructura."""
    s = live.get("signals", {})
    facts = [f"familia: <code>{esc(live.get('family',''))}</code>",
             f"ruta: <code>{esc(live.get('path',''))}</code>",
             f"madurez (del código): <b>{esc(live.get('maturity',''))}</b>",
             f"tamaño: {s.get('loc','?')} líneas",
             f"último commit: {esc(s.get('last_commit','') or '—')}",
             f"última prueba: {esc(s.get('last_tested','') or '—')}"]
    out = ("<h4>Señal viva (medida del código)</h4><ul>"
           + "".join(f"<li>{f}</li>" for f in facts) + "</ul>")
    path = live.get("path", "")
    if path and not path.startswith("/"):
        out += (
            f"<button class='code-btn' onclick=\"loadCode('{esc(path)}', this)\">"
            f"📄 Ver / editar código</button>"
            "<div class='code-area' hidden>"
            "<div class='code-tools'>"
            "<button class='ct' onclick='reviewCode(this)'>🔍 Revisar (guards + crítica local)</button>"
            "<button class='ct apply' onclick='applyCode(this)'>💾 Aplicar</button>"
            "<button class='ct rev' onclick='revertCode(this)'>↩︎ Revertir</button>"
            "<span class='code-status'></span></div>"
            f"<textarea class='codeview' data-path='{esc(path)}' data-hash='' spellcheck='false'></textarea>"
            "<div class='code-report'></div></div>")
    return out + _render_structure(live)


def _tested_html(live: dict | None, s: dict) -> str:
    """Chip de 'última prueba' (fecha si tiene test propio; 'sin prueba'; o vacío si curado)."""
    tested = s.get("last_tested") or ""
    if tested:
        return f'<span class="tested ok">🧪 probado {esc(tested)}</span>'
    return '<span class="tested no">🧪 sin prueba propia</span>' if live else ""


def _render_component(comp: dict, live_idx: dict, cid: str) -> str:
    """Tarjeta: enunciado + estado + última prueba; clic → bullets; botón → modal científico."""
    live = match_live(comp, live_idx)
    icon, color = _FAMILY_META.get((live or {}).get("family", ""), ("•", "#6e7781"))
    raw_mat = (live or {}).get("maturity", "") or "curado"
    mat_color = _MAT_COLOR.get((live or {}).get("maturity", ""), "#9a6dd7")
    s = (live or {}).get("signals", {})
    tested_html = _tested_html(live, s)
    enunciado = _first_sentence(comp.get("what_for", ""))
    bullets = "".join(f"<li>{esc(b)}</li>" for b in _to_bullets(comp.get("what_for", ""))) \
        or f'<li>{esc(comp.get("what_for", "") or "—")}</li>'
    return f"""<div class="comp" data-name="{esc(comp.get('name','')).lower()}" data-status="{esc(raw_mat)}">
<div class="comp-h" style="border-left-color:{color}" onclick="this.parentNode.classList.toggle('open')">
<span class="tog">▸</span><span class="fam-ic" style="background:{color}1f;color:{color}">{icon}</span>
<span class="comp-nm">{esc(comp.get('name'))}</span>
<span class="status-chip" style="background:{mat_color}">{esc(_MAT_LABEL.get(raw_mat, raw_mat))}</span>
{tested_html}{_scope_badge(comp)}
<span class="enunciado">{esc(enunciado)}</span></div>
<div class="comp-d">
<div class="sem-lbl">¿Para qué sirve?</div>
<ul class="semantic">{bullets}</ul>
<button class="sci-btn" onclick="openModal(event,'{cid}')">🔬 Ver explicación científica completa</button>
<div class="sci-src" id="{cid}" hidden>{_sci_content(comp, live)}</div>
</div></div>"""


def _maturity_counts(live: dict) -> dict[str, int]:
    """Conteo de componentes vivos por madurez."""
    out: dict[str, int] = {}
    for c in live.get("components", []):
        out[c.get("maturity", "")] = out.get(c.get("maturity", ""), 0) + 1
    return out


_LIVE_GROUP = {
    "script": ("Scripts de operación (.sh)", "Tareas de shell: hooks, backups, vacuum, wrappers, instalador."),
    "skill": ("Skills (/aris-* + Claude Code)",
              "Comandos que la sesión invoca a mano: los /aris-* propios y las skills de Claude Code "
              "(status, preflight, second-auditor, pdf, xlsx…) que el amplificador usa."),
    "config": ("Config / manifiestos", "Archivos que cablean y configuran el plugin."),
    "artifact": ("Artefactos / datos / documentos", "Carpetas de datos, plantillas, corpus y docs."),
    "tool": ("Herramientas extra", "Utilidades vivas no listadas 1:1 en el curado."),
    "engine": ("Motor — piezas extra", "Módulos del motor no listados 1:1 en el curado."),
    "hook_handler": ("Hooks/handlers extra", "Revisores del dispatcher no listados 1:1."),
    "database": ("Bases de datos", "Almacenes locales/externos."),
}


def _unmatched_by_family(groups: list[dict], live_idx: dict, live: dict) -> dict[str, list[dict]]:
    """Componentes vivos que ningún componente curado casó, agrupados por familia."""
    keys: set[str] = set()
    for g in groups:
        for c in g.get("components", []):
            m = match_live(c, live_idx)
            if m:
                keys.add(m["name"])
    by_fam: dict[str, list[dict]] = {}
    for c in live.get("components", []):
        if c["name"] not in keys:
            by_fam.setdefault(c["family"], []).append(c)
    return by_fam


def _render_live_only(groups: list[dict], live_idx: dict, live: dict, cid: int) -> tuple[str, int]:
    """Renderiza, DENTRO del inventario, las piezas vivas no listadas en el curado.

    Sintetiza un componente curado mínimo (desde la señal viva) para reusar la misma tarjeta.
    """
    by_fam = _unmatched_by_family(groups, live_idx, live)
    if not by_fam:
        return "", cid
    blocks = []
    for fam in sorted(by_fam, key=lambda f: -len(by_fam[f])):
        label, where = _LIVE_GROUP.get(fam, (fam, "Piezas vivas de esta familia."))
        rows = []
        for c in sorted(by_fam[fam], key=lambda x: x["name"]):
            synth = {"name": c["name"],
                     "what_for": c.get("role", "") or "Pieza viva detectada del código.",
                     "tech_detail": "", "status": c.get("maturity", ""),
                     "evidence": c.get("path", "")}
            rows.append(_render_component(synth, live_idx, f"sci-{cid}"))
            cid += 1
        blocks.append(f"""<div class="grp open">
<h3 class="grp-h" onclick="this.parentNode.classList.toggle('open')">
<span class="tog">▾</span> {esc(label)} <small>({len(by_fam[fam])})</small></h3>
<div class="grp-where">📍 {esc(where)}</div>
<div class="grp-body">{''.join(rows)}</div></div>""")
    intro = ("""<h2 style="margin-top:2rem">Piezas vivas del código (detectadas, no listadas 1:1 en el curado)</h2>
<p class="hint">El generador las leyó del código. El HTML curado las agrupa o aún no las lista una por una;
la única <b>genuinamente nueva</b> es <code>recall_usefulness</code>. Nada queda fuera.</p>""")
    return intro + "".join(blocks), cid


def _render_inventario(groups: list[dict], live_idx: dict, live: dict) -> str:
    """Inventario: barra de salud + buscador + grupos (dónde→para qué) con drill-down."""
    counts = _maturity_counts(live)
    health = "".join(
        f'<button class="hb" data-f="{esc(k)}"><b style="color:{_MAT_COLOR.get(k,"#6e7781")}">{v}</b> '
        f'{esc(_MAT_LABEL.get(k, k))}</button>'
        for k, v in sorted(counts.items(), key=lambda x: -x[1]) if k)
    blocks = []
    cid = 0
    for g in groups:
        rows = []
        for c in g.get("components", []):
            rows.append(_render_component(c, live_idx, f"sci-{cid}"))
            cid += 1
        blocks.append(f"""<div class="grp open">
<h3 class="grp-h" onclick="this.parentNode.classList.toggle('open')">
<span class="tog">▾</span> {esc(g.get('group_name'))}
<small>({len(g.get('components',[]))})</small></h3>
<div class="grp-where">📍 {esc(g.get('where_it_works'))}</div>
<div class="grp-body">{''.join(rows)}</div></div>""")
    lo_html, _ = _render_live_only(groups, live_idx, live, cid)
    return f"""<section id="inv" class="sec">
<h2>Inventario — qué partes tiene y para qué sirve cada una</h2>
<div class="health"><span class="health-l">Salud (clic para filtrar):</span>
<button class="hb active" data-f="all"><b style="color:#3fb950">todas</b></button>{health}</div>
<input id="search" class="search" placeholder="🔎 Buscar pieza por nombre…" oninput="filterComps()">
<p class="hint">Agrupado por <b>dónde funciona</b>. Clic en una pieza → para-qué en bullets; el botón 🔬 abre el detalle científico.</p>
{''.join(blocks)}{lo_html}</section>"""


def _render_table_block(tb: dict) -> str:
    """Renderiza una tabla {headers, rows} con scroll horizontal en móvil."""
    head = "".join(f"<th>{esc(x)}</th>" for x in tb.get("headers", []))
    body = "".join("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>"
                   for r in tb.get("rows", []))
    return (f"<div class='tbl-wrap'><table class='cmp'><thead><tr>{head}</tr></thead>"
            f"<tbody>{body}</tbody></table></div>")


def _render_block(b: dict) -> str:
    """Renderiza un bloque de contenido por tipo (bullets/kv/table/text)."""
    t = b.get("type")
    h = f'<h3>{esc(b.get("heading", ""))}</h3>' if b.get("heading") else ""
    if t == "bullets":
        return h + "<ul class='sec-ul'>" + "".join(
            f"<li>{esc(x)}</li>" for x in b.get("bullets", [])) + "</ul>"
    if t == "kv":
        return h + "<dl class='kv'>" + "".join(
            f"<dt>{esc(p.get('k',''))}</dt><dd>{esc(p.get('v',''))}</dd>"
            for p in b.get("kv", [])) + "</dl>"
    if t == "table":
        return h + _render_table_block(b.get("table", {}))
    if t == "text":
        return h + f"<p>{esc(b.get('text', ''))}</p>"
    return h


def _render_config() -> str:
    """⚙️ Config: config efectiva de ARIS4U (modelo, flags env, MCP cableados, settings_path)."""
    return _render_live_section(
        "config", "⚙️ Config — configuración efectiva de ARIS4U",
        "Modelo por defecto, flags de entorno, MCP cableados (repo/global) y ruta del "
        "<code>settings.json</code>. Read-only — la fuente es <code>tools/aris_config.collect()</code>, "
        "la misma que usa el skill <code>/aris-config</code>.",
        "<div id='cfg-body'></div>")


def _render_api() -> str:
    """🗺️ API: manifiesto de la consola — los ~20 brazos que Claude puede consultar."""
    return _render_live_section(
        "api", "🗺️ API — la superficie que Claude puede consultar",
        "Todos los endpoints de la Live Console: qué consultar, qué devuelve y qué tools MCP "
        "expone. Es la vista humana del <code>GET /manifest</code> — para que un humano vea "
        "la superficie nativa de ARIS4U de un tiro.",
        "<div id='api-body'></div>")


def _render_terminales() -> str:
    """Sección Terminales: xterm.js + PTY local (shell / Claude CLI / modelo local)."""
    return """<section id="term" class="sec">
<h2>Terminales</h2>
<p class="hint">Terminal local en tu Mac — corre con <b>tus permisos</b>, solo en <b>127.0.0.1</b>
(no expuesta a la red). Elige qué abrir:</p>
<div class="term-bar">
<button class="tbtn" onclick="startTerm('shell')">⌨️ Shell (zsh)</button>
<button class="tbtn" onclick="startTerm('claude')">🤖 Claude (CLI nativa)</button>
<button class="tbtn" onclick="startTerm('local')">🧠 Modelo local (qwen3.6:35b)</button>
<button class="tbtn alt" onclick="killTerm()">✕ Cerrar sesión</button>
</div>
<div id="termwrap"><div id="xterm"></div></div>
<p id="term-note" class="hint"></p></section>"""


def _render_live_section(sid: str, title: str, intro: str, body: str) -> str:
    """Caparazón de una sección de CONDUCTA viva (la rellena el JS vía fetch al servidor)."""
    return f"""<section id="{sid}" class="sec">
<h2>{esc(title)}</h2><p class="hint">{intro}</p>
<div class="live-offline" hidden>Esta sección lee el estado VIVO de ARIS4U y necesita el servidor:
<code>python3 -m aris4u_console.server</code></div>
{body}</section>"""


_ATOM_TABS = [
    ("atomos", "Catálogo"), ("atomos-func", "Función"), ("backlog", "Proyecto"),
    ("plantillas", "Plantillas"),
]


def _atom_tabs(active: str) -> str:
    """Barra de tabs compartida que unifica las 6 vistas de átomos en un solo menú."""
    btns = "".join(
        f'<button class="atab{" active" if sid == active else ""}" data-s="{sid}" '
        f"onclick=\"goAtomTab('{sid}')\">{esc(lbl)}</button>"
        for sid, lbl in _ATOM_TABS
    )
    return f'<div class="atom-tabs">{btns}</div>'


def _render_atom_section(sid: str, title: str, intro: str, body: str) -> str:
    """Sección de átomos = caparazón vivo + la barra de tabs unificada arriba."""
    return _render_live_section(sid, title, intro, _atom_tabs(sid) + body)


_CAP_TABS = [
    ("cap-skills", "Skills"), ("cap-agents", "Agents"),
    ("cap-mcp", "MCP"), ("cap-api", "API"),
]


def _cap_tabs(active: str) -> str:
    """Barra de 4 tabs de Capacidades (Skills→Agents→MCP→API), misma estética que átomos."""
    btns = "".join(
        f'<button class="atab{" active" if sid == active else ""}" data-s="{sid}" '
        f"onclick=\"goCapTab('{sid}')\">{esc(lbl)}</button>"
        for sid, lbl in _CAP_TABS
    )
    return f'<div class="atom-tabs">{btns}</div>'


def _render_cap_section(sid: str, title: str, intro: str, list_id: str) -> str:
    """Sección de una categoría de capacidad: tabs + resumen + botón probar + lista."""
    cat = sid.replace("cap-", "")
    body = (_cap_tabs(sid)
            + f'<div id="{list_id}-sum" class="live-cards"></div>'
            + '<div class="cap-actions"><button class="hb" '
            + f"onclick=\"testCap('{cat}','{list_id}')\">🧪 Probar que funcionan</button>"
            + f'<span id="{list_id}-test" class="hint"></span></div>'
            + f'<div id="{list_id}"></div>')
    return _render_live_section(sid, title, intro, body)


def _render_capacidades() -> str:
    """Las 4 secciones de Capacidades (una por tab) con su audit por valor."""
    return (
        _render_cap_section(
            "cap-skills", "🧰 Capacidades · Skills",
            "Skills de Claude Code y de ARIS4U, con audit por valor "
            "(estado · uso · redundancia · veredicto). El uso de skills no está "
            "instrumentado todavía → su veredicto se basa en estado + redundancia. "
            "<b>Scope:</b> aquí se cuentan TODAS las skills disponibles (incluidas las que traen "
            "los plugins instalados); el panel 🧩 Inventario cuenta solo las ~24 skills de usuario "
            "en <code>~/.claude/skills</code>. Por eso los totales difieren — es por alcance, no un error.",
            "capskills")
        + _render_cap_section(
            "cap-agents", "🧰 Capacidades · Agents",
            "Agent types con USO REAL medido en la telemetría (invocaciones de subagent). "
            "Ocioso = existe pero no se ha invocado.",
            "capagents")
        + _render_cap_section(
            "cap-mcp", "🧰 Capacidades · MCP",
            "MCP servers cableados y su uso real (mcp_call): los de Claude y el server "
            "propio de ARIS4U con sus tools.",
            "capmcp")
        + _render_cap_section(
            "cap-api", "🧰 Capacidades · API",
            "Modelos de Claude (la cognición rentada) + la API de ARIS4U: endpoints de "
            "la consola por tipo y los hooks cableados con su uso real.",
            "capapi")
    )


def _render_atomos_func() -> str:
    """🧬 Átomos por FUNCIÓN: agrupados por artifact_type (qué patrón de software resuelve)."""
    return _render_atom_section(
        "atomos-func",
        "🧬 Átomos por función — agrupados por tipo de patrón",
        "Los átomos agrupados por <code>artifact_type</code> (familia de patrón de software). "
        "Responde: ¿qué tipos de patrón tengo y cuántos de cada uno? Expande una familia para ver "
        "sus átomos.",
        "<div id='atomfunc-list'></div>")


def _render_atomos() -> str:
    """🧬 Átomos de método: patrones reutilizables con su composición/uso (clic → detalle)."""
    return _render_atom_section(
        "atomos", "🧬 Átomos de método — los patrones reutilizables de ARIS4U",
        "Cada átomo es un patrón de software indexado por ESTRUCTURA (no por dominio) para "
        "reusarlo entre proyectos. Clic en uno → qué es, cómo se compone, para qué sirve y en "
        "qué proyectos se usa.",
        "<div id='atoms-filters' class='mem-filters'></div><div id='atoms-list'></div>")


def _render_valorizacion() -> str:
    """💎 Valorización RICE-A+Moat: puntúa cada átomo y da veredicto adopt/build/omit."""
    return _render_atom_section(
        "valoriz",
        "💎 Valorización — RICE-A + Moat · ¿qué vale adoptar?",
        "Cada átomo estructural recibe un score RICE-A = (Reach × Impact × Confidence × "
        "Adoption) / Effort y un Moat (proyectos a los que transfiere). El veredicto "
        "<b>adopt</b> = listo y transferible; <b>build</b> = promisorio, documentar más; "
        "<b>omit</b> = bajo valor ahora.",
        "<div id='valoriz-summary' class='live-cards'></div>"
        "<div id='valoriz-filters' class='mem-filters'></div>"
        "<div id='valoriz-list'></div>")


def _render_auditoria() -> str:
    """🔎 Auditoría del store de átomos: duplicados, huecos, bajo-valor, sin metadatos."""
    return _render_atom_section(
        "auditoria",
        "🔎 Auditoría del catálogo de átomos",
        "Detecta problemas de calidad en el store: duplicados por "
        "<code>structural_signature</code>, átomos sin <code>validity_domain</code> "
        "(no se sabe dónde aplica/rompe), sin <code>source_project</code>, marcados "
        "[BAJO VALOR]/[DESCARTADO], y huecos (problem_class sin uso real).",
        "<div id='audit-summary' class='live-cards'></div>"
        "<div id='audit-findings'></div>")


def _render_backlog() -> str:
    """📋 Backlog de adopción: patrones probados agrupados por proyecto destino.

    Incluye un encabezado de verificación (fit_totals chips), una nota sobre
    el significado de "candidato", la lista colapsada de proyectos descartados
    y el acordeón de candidatos por proyecto.
    """
    return _render_atom_section(
        "backlog",
        "📋 Backlog — patrones probados listos para adoptar",
        "Resultado accionable de Valorización: qué patrón PROBADO debería adoptar cada "
        "proyecto destino, ordenado por score RICE-A. Adoptalo de mayor a menor score. "
        "Cada grupo es un proyecto destino; expande para ver los patrones y de dónde vienen.",
        "<div id='backlog-summary' class='live-cards'></div>"
        "<div id='backlog-fit-chips'></div>"
        "<div id='backlog-filtered'></div>"
        "<div id='backlog-list'></div>")


def _render_estado() -> str:
    """A0 — Tablero de ESTADO: cada parte en verde/naranja/rojo (la vista 'JARVIS')."""
    return """<section id="estado" class="sec">
<h2>🟢 Estado — salud de cada parte de ARIS4U</h2>
<p class="hint"><span class="st-key ok">verde</span> funciona ·
<span class="st-key warn">naranja</span> necesita trabajo ·
<span class="st-key down">rojo</span> roto. ARIS4U se autoexamina; lo mismo que ves aquí
lo consulto yo (Claude) de un tiro, sin inspeccionar a mano.</p>
<div class="live-offline" hidden>Necesita el servidor: <code>python3 -m aris4u_console.server</code></div>
<div id="st-overall"></div><div id="st-items" class="st-grid"></div></section>"""


def _render_memoria() -> str:
    """A1 — Panel de memoria: qué recuerda ARIS4U (decisiones/guards/digests + recall)."""
    return _render_live_section(
        "memoria", "🧠 Memoria — qué recuerda ARIS4U",
        "Lo que ARIS4U tiene guardado y recupera por cliente (de <code>data/sessions.db</code>), "
        "más el medidor de utilidad del recall. Es el exocórtex en números reales.",
        "<div id='mem-cards' class='live-cards'></div>"
        "<div id='mem-clients'></div>"
        "<h3>Explorar la memoria por proyecto</h3>"
        "<p class='hint'>Filtra por <b>proyecto/cliente</b>, por <b>dominio</b> (≈ área del "
        "proyecto) o busca texto. <code>locked</code> = decisiones que no se contradicen; "
        "<code>stale</code> = sin tocar hace &gt;30 días. (Separación real 1-sesión-2-proyectos "
        "= fase 2 en el motor.)</p>"
        "<div class='mem-filters'>"
        "<input id='mem-q' class='search mem-qbox' placeholder='🔎 Buscar en decisiones y guards…' "
        "oninput='memSearchDebounced()'>"
        "<select id='mem-client' class='mem-sel' onchange='memSearch()'>"
        "<option value=''>Todos los proyectos</option></select>"
        "<select id='mem-domain' class='mem-sel' onchange='memSearch()'>"
        "<option value=''>Todos los dominios</option></select>"
        "<select id='mem-type' class='mem-sel' onchange='memSearch()'>"
        "<option value=''>Todos los tipos</option></select>"
        "<button class='hb' id='mf-locked' onclick='memToggle(this)'>🔒 locked</button>"
        "<button class='hb' id='mf-stale' onclick='memToggle(this)'>🕒 stale &gt;30d</button>"
        "</div>"
        "<div id='mem-count' class='hint'></div>"
        "<div id='mem-results'></div>")


def _render_pulso() -> str:
    """A2 — Telemetría en vivo: ver a ARIS4U pensar (stream del log de eventos)."""
    return _render_live_section(
        "pulso", "📡 Pulso — ver a ARIS4U pensar",
        "El flujo de eventos en vivo: qué hooks corren, recalls, model_hint, guards que bloquean "
        "(de <code>logs/v16.1-events.jsonl</code>).",
        "<div class='pulse-bar'>"
        "<button class='tbtn' onclick='startPulse(this)'>▶︎ En vivo</button>"
        "<button class='tbtn alt' onclick='stopPulse()'>⏸ Pausar</button>"
        "<span id='pulse-state' class='hint'></span></div>"
        "<div id='tel-agg' class='live-cards'></div>"
        "<div id='tel-feed' class='feed'></div>")


def _render_hooks() -> str:
    """A4 — Estado de hooks/guards: qué está cableado y qué dispara."""
    return _render_live_section(
        "hooks", "🛡️ Hooks y guards — qué está cableado y qué dispara",
        "Los hooks que ARIS4U corre en cada evento de Claude Code (repo + global) y, de la "
        "telemetría, qué fuente disparó (dispatcher, guards, MCP).",
        "<div id='phi-guard-metric' class='live-cards'></div>"
        "<div id='hooks-body'></div><div id='hooks-fired'></div>")


def _render_routing() -> str:
    """💸 Routing — observatorio de gobierno de modelos (V18 disciplina de model=)."""
    return _render_live_section(
        "routing", "💸 Routing — gobierno de modelos",
        "Disciplina de <code>model=</code> en los <code>Agent()</code>: qué fracción "
        "especificó el modelo explícito (target ≥80%), distribución por tier y costo "
        "relativo estimado. Fuente: event log de los últimos 7 días.",
        "<div id='rt-alert'></div>"
        "<div id='rt-cards' class='live-cards'></div>"
        "<div id='rt-bymodel'></div>"
        "<div id='rt-bysubagent'></div>"
        "<div id='rt-byintent'></div>")


def _render_plantillas() -> str:
    """📐 Plantillas: catálogo de skeletons reutilizables agrupados por familia (artifact_type).

    Los datos vienen del endpoint GET /skeletons. El código de cada skeleton se inyecta
    en el DOM via textContent (nunca innerHTML) para evitar XSS y escape-hell.
    """
    return _render_atom_section(
        "plantillas",
        "📐 Plantillas — código reutilizable que el build flow inyecta",
        "Lo que ARIS4U inyecta cuando construyes en un dominio que matchea un patrón probado: "
        "plantillas SQL/TS/shell con <code>&lt;placeholders&gt;</code> reemplazables. "
        "Agrupadas por familia de problema (artifact_type); expande para ver el skeleton completo.",
        "<div id='skel-summary' class='live-cards'></div>"
        "<div id='skel-list'></div>")


def _render_amplificador() -> str:
    """Cabina del amplificador F1: cuerpo, ROI, progreso N/30 y etiquetado con un clic."""
    return _render_live_section(
        "amp", "🔬 Amplificador — el cuerpo local que potencia a Claude",
        "El lazo <b>usar → medir → cablear</b>: el cuerpo local estructura/critica, se mide si "
        "ayuda, y a las 30 etiquetas se cablea la capa de decisión (§8.5). Etiqueta con un clic.",
        "<div id='amp-head' class='live-cards'></div>"
        "<div id='amp-progress'></div>"
        "<h3>Pendientes de etiquetar — ¿ayudó?</h3>"
        "<div id='amp-pending'></div>")


def _render_calidad() -> str:
    """📊 Calidad: historial del code-quality gate por módulo (deuda técnica real)."""
    return _render_live_section(
        "calidad", "📊 Calidad — deuda técnica del gate de código",
        "El gate de calidad (ruff/radon) corre en cada edición; aquí su historial de "
        "<code>gate_results</code>. Valida que el gate FUNCIONA y dónde se acumula la deuda: "
        "los módulos con más <code>issues</code> primero.",
        "<div id='qual-summary' class='live-cards'></div>"
        "<div id='qual-list'></div>")


def _render_briefs() -> str:
    """🧵 Briefs: resúmenes de sesión recientes (el contexto para cebar a Claude rápido)."""
    return _render_live_section(
        "briefs", "🧵 Briefs de sesión — contexto validado para cebar a Claude",
        "Los resúmenes de sesiones recientes (de <code>claude-mem.db</code>): qué se pidió, qué se "
        "aprendió, qué se completó. Es el brief que alimenta a Claude rápido y validado al iniciar "
        "una sesión nueva.",
        "<div id='briefs-list'></div>")


def _render_uipipe() -> str:
    """🎨 UI/UX Pipeline: mapea el plugin de gobernanza de UI (PLAN/BUILD/VERIFY)."""
    phases = [
        ("PLAN", "skill ui-plan-review",
         "Critica el plan de una pantalla ANTES de codear: contrato A–G, detecta 'AI slop' "
         "(todo centrado, KPIs sin comparativo, filtros sin lógica), fuerza decisiones explícitas."),
        ("BUILD", "hook inject-design-contract + shadcn MCP",
         "Al tocar *.tsx inyecta el DESIGN_CONTRACT una vez por sesión; shadcn MCP sirve props "
         "reales (anti-alucinación de props)."),
        ("VERIFY", "/ui-review + agente ui-design-reviewer",
         "Gate de cierre: a11y mecánico + el agente abre la UI viva en el navegador y la recorre "
         "contra el contrato A–G con severidades [Blocker/High/Medium/Nitpick]."),
    ]
    cards = "".join(
        f'<div class="uip-phase"><div class="uip-ph">{esc(p)}</div>'
        f'<div class="uip-tool">{esc(tool)}</div><div class="uip-desc">{esc(desc)}</div></div>'
        for p, tool, desc in phases)
    body = (f'<div class="uip-flow">{cards}</div>'
            '<p class="hint">Estado: instalado y cableado (hook PreToolUse + 2 skills + agente + '
            'comando <code>/ui-review</code> + shadcn MCP). Complementa <code>frontend-design</code> '
            '(estética); NO solapa con ARIS4U (que no tiene capa de UI).</p>')
    return _render_live_section(
        "uipipe", "🎨 UI/UX Pipeline — gobernanza de interfaces (plugin)",
        "El plugin <code>aris4u-ui-pipeline</code> gobierna la construcción de UI en 3 fases, "
        "cubriendo lo estructural/funcional (jerarquía, navegación, botones conectados, filtros "
        "con lógica) que el contrato anti-slop exige.",
        body)


def _render_intake() -> str:
    """📥 Nuevo proyecto — superficie de intake para el no-técnico.

    Un CEO/fundador describe lo que quiere (brief) + sube docs de soporte.
    El formulario hace POST a /intake (JSON puro; docs en base64).
    La lista inferior muestra los intakes existentes vía GET /intakes.
    """
    return """<section id="intake" class="sec">
<h2>📥 Nuevo proyecto</h2>
<p class="hint">Describe lo que quieres construir. No hace falta ser técnico: escribe en
tus palabras. Si tienes documentos (requisitos, ejemplos, contexto) adjúntalos. El equipo
ARIS4U lo convierte en un plan de build.</p>
<div class="live-offline" hidden>Necesita el servidor:
<code>python3 -m aris4u_console.server</code></div>

<div class="proj-intk-form">
<label class="hint" style="display:block;margin-bottom:.5rem">
  Nombre de tu proyecto o empresa:
  <input id="intk-client-friendly" class="search"
    style="width:260px;margin-left:.4rem"
    placeholder="Ej: Acme Corp, Mi Clínica, Startup XYZ"
    oninput="updateIntakeSlug()">
  <span id="intk-slug-preview" class="hint"
    style="margin-left:.5rem;font-family:monospace;color:#6e7781"></span>
  <input id="intk-client" type="hidden">
  <datalist id="intk-client-list"></datalist>
</label>
<label class="hint" style="display:block;margin-bottom:.5rem">
  Brief — describe lo que necesitas:
  <textarea id="intk-brief" class="intk-ta"
    placeholder="Ej: Quiero un CRM simple para mi clínica con agenda, historial de pacientes y alertas de citas."
    rows="5"></textarea>
</label>
<label class="hint" style="display:block;margin-bottom:.5rem">
  Documentos de soporte (opcional, máx 2 MB/archivo):
  <input id="intk-files" type="file" multiple
    accept=".txt,.md,.pdf,.csv,.json,.yaml,.yml,.toml,.rst,.html,.htm"
    style="display:block;margin-top:.3rem">
</label>
<button class="ct" onclick="submitIntake(this)" style="margin-top:.3rem">&#43; Enviar brief</button>
<span id="intk-status" class="hint" style="margin-left:.8rem"></span>
</div>

<h3 style="margin-top:1.4rem">Intakes recientes</h3>
<div id="intk-list"><p class="hint">Cargando…</p></div>

<script>
(function(){
  function _toSlug(text){
    /* Derive a valid slug ([a-z0-9_-]) from free-text the CEO typed. */
    return text.trim().toLowerCase()
      .replace(/[áàäâ]/g,'a').replace(/[éèëê]/g,'e')
      .replace(/[íìïî]/g,'i').replace(/[óòöô]/g,'o')
      .replace(/[úùüû]/g,'u').replace(/ñ/g,'n')
      .replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,60)||'';
  }
  function updateIntakeSlug(){
    var friendly=(document.getElementById('intk-client-friendly').value||'');
    var slug=_toSlug(friendly);
    var hidden=document.getElementById('intk-client');
    var preview=document.getElementById('intk-slug-preview');
    if(hidden)hidden.value=slug;
    if(preview){
      preview.textContent=slug?('tu proyecto: '+slug):'';
      preview.style.color=slug?'#1a7f37':'#6e7781';
    }
  }
  window.updateIntakeSlug=updateIntakeSlug;
  function loadIntakeClients(){
    fetch('/memory/facets',{headers:{'Host':'localhost'}})
      .then(r=>r.json()).then(d=>{
        var dl=document.getElementById('intk-client-list');
        if(!dl)return;
        (d.clients||[]).forEach(function(c){
          var o=document.createElement('option');o.value=c;dl.appendChild(o);});
      }).catch(function(){});
  }
  function approveIntake(intakeId,btn){
    btn.disabled=true;btn.textContent='Iniciando…';
    fetch('/run-intake',{method:'POST',headers:{'Content-Type':'application/json','Host':'localhost'},
      body:JSON.stringify({intake_id:intakeId})})
      .then(r=>r.json()).then(function(d){
        if(d.ok){btn.textContent='En construcción';btn.style.opacity='0.5';loadIntakes();}
        else{btn.disabled=false;btn.textContent='▶ Aprobar y construir';
          alert('Error: '+_esc2(d.error||'desconocido'));}
      }).catch(function(e){btn.disabled=false;btn.textContent='▶ Aprobar y construir';
        alert('Error de red: '+_esc2(String(e)));});
  }
  window.approveIntake=approveIntake;
  function loadIntakes(){
    var el=document.getElementById('intk-list');
    if(!el)return;
    fetch('/intakes',{headers:{'Host':'localhost'}})
      .then(r=>r.json()).then(function(d){
        var items=d.intakes||[];
        if(!items.length){el.innerHTML='<p class="hint">Sin intakes aún.</p>';return;}
        var h='<table class="hk-tbl"><thead><tr><th>ID</th><th>Cliente</th>'
          +'<th>Estado</th><th>Brief</th><th>Fecha</th><th>Acción (operador)</th></tr></thead><tbody>';
        items.forEach(function(it){
          var badge=it.status==='done'?'<span class="proj-tag proj-dec">'+_esc2(it.status_label||it.status)+'</span>'
            :it.status==='rejected'?'<span class="proj-tag proj-gate">'+_esc2(it.status_label||it.status)+'</span>'
            :'<span class="proj-tag proj-dig">'+_esc2(it.status_label||it.status)+'</span>';
          var preview=it.brief_preview?'<span class="hint" title="'+_esc2(it.brief_preview)+'">'+_esc2((it.brief_preview||'').slice(0,60))+(it.brief_preview.length>60?'…':'')+'</span>':'—';
          var action=it.status==='pending'
            ?'<button class="ct" style="font-size:.8rem;padding:.2rem .6rem" onclick="approveIntake('+_esc2(it.id)+',this)">▶ Aprobar y construir</button>'
            :'<span class="hint">—</span>';
          h+='<tr><td>'+_esc2(it.id)+'</td><td>'+_esc2(it.client_id)+'</td>'
            +'<td>'+badge+'</td><td>'+preview+'</td>'
            +'<td>'+_esc2((it.created_at||'').slice(0,19).replace('T',' '))+'</td>'
            +'<td>'+action+'</td></tr>';
        });
        h+='</tbody></table>';
        el.innerHTML=h;
      }).catch(function(){el.innerHTML='<p class="hint">Sin conexión al servidor.</p>';});
  }
  function submitIntake(btn){
    /* Read slug from the hidden field (derived by updateIntakeSlug from the friendly name). */
    var client=(document.getElementById('intk-client').value||'').trim();
    var brief=(document.getElementById('intk-brief').value||'').trim();
    var st=document.getElementById('intk-status');
    if(!client){st.textContent='Escribe el nombre de tu proyecto o empresa.';return;}
    if(!brief){st.textContent='Describe lo que necesitas en el campo Brief.';return;}
    btn.disabled=true; st.textContent='Enviando…';
    var files=document.getElementById('intk-files').files||[];
    var readers=[]; var docs=[];
    function _send(){
      fetch('/intake',{method:'POST',headers:{'Content-Type':'application/json','Host':'localhost'},
        body:JSON.stringify({client:client,brief:brief,docs:docs})})
        .then(r=>r.json()).then(function(d){
          btn.disabled=false;
          if(d.ok){st.textContent=(d.status_label||'Brief enviado')+' (intake #'+d.intake_id+').';
            if(d.next_step){var ns=document.createElement('p');ns.className='hint';
              ns.textContent=d.next_step;st.parentNode&&st.parentNode.insertBefore(ns,st.nextSibling);}
            document.getElementById('intk-brief').value='';
            document.getElementById('intk-files').value='';
            loadIntakes();}
          else{st.textContent='Error: '+_esc2(d.error||'desconocido');}
        }).catch(function(e){btn.disabled=false;st.textContent='Error de red: '+_esc2(String(e));});
    }
    if(!files.length){_send();return;}
    var pending=files.length;
    Array.from(files).forEach(function(f){
      var r=new FileReader();
      r.onload=function(ev){
        var b64=btoa(String.fromCharCode.apply(null,new Uint8Array(ev.target.result)));
        docs.push({name:f.name,content_b64:b64});
        if(--pending===0)_send();
      };
      r.readAsArrayBuffer(f);
    });
  }
  window.submitIntake=submitIntake;
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',function(){loadIntakeClients();loadIntakes();});
  } else { loadIntakeClients(); loadIntakes(); }
})();
</script>
</section>"""


def _render_proyecto() -> str:
    """🔨 Proyecto/Cowork — vista de build en vivo para el no-técnico.

    Muestra el timeline de commits reales con el porqué anotado por ARIS4U
    (decisions/gates/digests), un canal de comentarios por commit y refresco
    automático vía SSE (/project/stream).
    """
    return _render_live_section(
        "proyecto",
        "🔨 Proyecto — build en vivo",
        "El avance REAL del build: cada commit en git con el porqué anotado por ARIS4U "
        "(decisiones, gates, digests). Git es el ancla — un commit sin anotación se muestra "
        "igual. Comenta por commit; el feed se refresca en vivo vía SSE.",
        "<datalist id='proj-client-list'></datalist>"
        "<div class='pulse-bar'>"
        "<label class='hint'>Proyecto: <input id='proj-client' class='search' "
        "list='proj-client-list' "
        "style='width:160px;margin-left:.4rem' value='aris4u' "
        "onchange='loadProyecto()' placeholder='nombre de tu proyecto'></label>"
        "<button class='tbtn' style='margin-left:1rem' onclick='loadProyecto()'>&#8635; Recargar</button>"
        "<span id='proj-state' class='hint' style='margin-left:.8rem'></span>"
        "</div>"
        "<div id='proj-timeline'></div>")


def _render_mcp() -> str:
    """A3 — Operar las MCP tools desde la pantalla (no solo verlas)."""
    return """<section id="mcp" class="sec">
<h2>🔭 MCP tools — operar, no solo ver</h2>
<p class="hint">Invoca los brazos opt-in de ARIS4U. <b>Lectura</b> = consulta (sin escribir; nota:
search/recall sí tocan el embedder local) · <b>local</b> = inferencia pesada del modelo local
(pide confirmar; puede estar frío) · <b>escribe</b> = modifica la memoria (pide confirmar).</p>
<div class="live-offline" hidden>Necesita el servidor: <code>python3 -m aris4u_console.server</code></div>
<div class="mcp-grid">
<div class="mcp-tool"><h3>aris_health <span class="kind read">lectura</span></h3>
<p class="hint">Salud del clúster (Ollama Mac+W2) + stats de la memoria.</p>
<button class="ct" onclick="callMcp('aris_health',[],this)">▶︎ Ejecutar</button></div>

<div class="mcp-tool"><h3>aris_search <span class="kind read">lectura</span></h3>
<input class="mcp-in" data-k="query" placeholder="qué buscar…">
<input class="mcp-in" data-k="client" placeholder="cliente (opcional)">
<button class="ct" onclick="callMcp('aris_search',['query','client'],this)">▶︎ Buscar</button></div>

<div class="mcp-tool"><h3>aris_recall_client <span class="kind read">lectura</span></h3>
<input class="mcp-in" data-k="client_name" placeholder="cliente (aris4u, client-b, client-c…)">
<input class="mcp-in" data-k="query" placeholder="filtro (opcional)">
<button class="ct" onclick="callMcp('aris_recall_client',['client_name','query'],this)">▶︎ Recordar</button></div>

<div class="mcp-tool"><h3>aris_structure <span class="kind local">local</span></h3>
<input class="mcp-in" data-k="idea" placeholder="idea cruda a estructurar…">
<button class="ct" onclick="callMcp('aris_structure',['idea'],this)">▶︎ Estructurar</button></div>

<div class="mcp-tool"><h3>aris_dialectic <span class="kind local">local</span></h3>
<input class="mcp-in" data-k="task" placeholder="tarea/código a revisar…">
<input class="mcp-in" data-k="file_path" placeholder="ruta de archivo (opcional)">
<button class="ct" onclick="callMcp('aris_dialectic',['task','file_path'],this)">▶︎ Revisar</button></div>

<div class="mcp-tool"><h3>aris_critique <span class="kind local">local</span></h3>
<input class="mcp-in" data-k="response" placeholder="respuesta/texto a criticar…">
<input class="mcp-in" data-k="angles" placeholder="ángulos (opcional)">
<button class="ct" onclick="callMcp('aris_critique',['response','angles'],this)">▶︎ Criticar</button></div>

<div class="mcp-tool"><h3>aris_ingest <span class="kind write">escribe</span></h3>
<input class="mcp-in" data-k="content" placeholder="decisión/guard a guardar…">
<input class="mcp-in" data-k="client" placeholder="cliente (opcional)">
<button class="ct apply" onclick="callMcp('aris_ingest',['content','client'],this)">▶︎ Guardar</button></div>
</div>
<div id="mcp-out" class="mcp-out"></div></section>"""


def _navgroup(gid: str, label: str, *items: str) -> str:
    """Grupo de navegación COLAPSABLE: cabecera clickable + sub-items (ocultos por defecto)."""
    inner = "".join(items)
    return (f'<div class="nav-group" id="{gid}">'
            f'<button class="nav-ghead" onclick="toggleNavGroup(this)">'
            f'<span>{label}</span><span class="nav-chev">▸</span></button>'
            f'<div class="nav-gbody">{inner}</div></div>')


def render_console_html(live: dict, curated: dict) -> str:
    """Construye la pantalla completa marida (humana + viva)."""
    identity = curated.get("identity", {})
    groups = curated.get("inventory", {}).get("groups", [])
    behavior = curated.get("behavior", {})
    live_idx = _live_index(live)

    def _nb(sid: str, label: str, *, active: bool = False) -> str:
        """Botón de navegación (zona OPERACIÓN/REFERENCIA/SISTEMA)."""
        return (f'<button class="nav-b{" active" if active else ""}" '
                f'data-s="{esc(sid)}">{label}</button>')

    # Menú de 5 entradas con submenús colapsables (IA de onboarding):
    #   Inicio (¿qué es?) → Componentes (¿de qué se compone?) → Salud (¿funciona?)
    #   → Conocimiento (¿qué sabe / con qué ceba a Claude?) → Operar (¿cómo se usa?).
    nav = [
        _nb("inicio", "🏠 Inicio", active=True),
        _navgroup("g-comp", "🧩 Componentes",
                  _nb("inv", "Inventario"), _nb("flow", "Comportamiento"),
                  _nb("amp", "🔬 Amplificador"), _nb("cap-skills", "🧰 Capacidades")),
        _navgroup("g-salud", "🩺 Salud",
                  _nb("estado", "🟢 Estado"), _nb("pulso", "📡 Pulso"),
                  _nb("calidad", "📊 Calidad"), _nb("hooks", "🛡️ Hooks"),
                  _nb("routing", "💸 Routing")),
        _navgroup("g-conoc", "🧠 Conocimiento",
                  _nb("memoria", "🧠 Memoria"), _nb("briefs", "🧵 Briefs"),
                  _nb("atomos", "🧬 Átomos")),
        _navgroup("g-operar", "🛠️ Operar",
                  _nb("intake", "📥 Nuevo proyecto"),
                  _nb("proyecto", "🔨 Proyecto"),
                  _nb("mcp", "🔭 MCP"), _nb("uipipe", "🎨 UI/UX"),
                  _nb("config", "⚙️ Config"), _nb("api", "🗺️ API"),
                  _nb("term", "⌨️ Terminales")),
    ]

    g = live.get("git", {})
    body = (_render_que_es(identity)
            + _render_estado() + _render_pulso() + _render_calidad() + _render_hooks()
            + _render_routing()
            + _render_inventario(groups, live_idx, live) + _render_comportamiento(behavior)
            + _render_amplificador() + _render_capacidades()
            + _render_memoria() + _render_briefs()
            + _render_atomos() + _render_atomos_func()
            + _render_backlog()
            + _render_plantillas()
            + _render_intake()
            + _render_proyecto()
            + _render_mcp() + _render_uipipe()
            + _render_config() + _render_api() + _render_terminales())
    return _PAGE.format(nav="".join(nav), body=body,
                        branch=esc(g.get("branch", "")), head=esc(g.get("head", "")),
                        gen=esc(live.get("generated_at", "")[:19]),
                        n=live.get("totals", {}).get("components", 0))


def main(argv: list[str] | None = None) -> int:
    """Genera out/console.html desde las dos capas."""
    ap = argparse.ArgumentParser(description="Render de la Live Console semántica")
    ap.add_argument("--live", type=Path, default=_LIVE)
    ap.add_argument("--curated", type=Path, default=_CURATED)
    ap.add_argument("--out", type=Path, default=_OUT)
    args = ap.parse_args(argv)
    live = json.loads(args.live.read_text(encoding="utf-8"))
    curated = json.loads(args.curated.read_text(encoding="utf-8"))
    args.out.write_text(render_console_html(live, curated), encoding="utf-8")
    print(f"Pantalla escrita en {args.out}")
    return 0


_PAGE = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ARIS4U — Consola Viva</title><style>
/* ===== ARIS4U Console — Dirección C "Terminal + Identidad" (oscuro, alto contraste AAA) ===== */
:root{{
  color-scheme:dark;
  --bg-base:#0d1117; --bg-1:#161b22; --bg-2:#1c2128; --bg-3:#262c36;
  --border:#30363d; --border-hi:#484f58;
  --text-1:#e6edf3; --text-2:#8b949e; --text-3:#6e7681;
  --ok:#3fb950; --warn:#d29922; --danger:#f85149; --action:#58a6ff;
  --mcp:#bc8cff; --hook:#ffa657; --info:#79c0ff;
  --nav-bg:#010409;
}}
*{{box-sizing:border-box}}
body{{font:16px/1.55 ui-sans-serif,-apple-system,system-ui,"Segoe UI",sans-serif;
color:var(--text-1);background:var(--bg-base);margin:0}}
.mono,code,.comp-nm,.timestamp{{font-family:ui-monospace,"SFMono-Regular",Menlo,Consolas,monospace;
font-feature-settings:"tnum" 1}}
:focus-visible{{outline:2px solid var(--action);outline-offset:2px}}
/* ---- header + status strip ---- */
header{{background:#010409;border-bottom:1px solid var(--border);padding:.55rem 1.1rem;
display:flex;align-items:center;gap:1rem;flex-wrap:wrap}}
header h1{{margin:0;font-size:1rem;font-weight:600;letter-spacing:.01em;display:flex;align-items:center;gap:.5rem}}
.live-dot{{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--ok);
box-shadow:0 0 0 0 transparent;animation:pulse-dot 2s ease-in-out infinite}}
@keyframes pulse-dot{{0%,100%{{opacity:1}}50%{{opacity:.35}}}}
.statusstrip{{font-family:ui-monospace,monospace;font-size:.8rem;color:var(--text-2);
display:flex;gap:1.1rem;flex-wrap:wrap;align-items:center}}
.statusstrip b{{color:var(--text-1);font-weight:600}}
.statusstrip .sep{{color:var(--border-hi)}}
.refresh-btn{{margin-left:auto;background:var(--bg-2);color:var(--text-1);border:1px solid var(--border);
border-radius:7px;padding:.32rem .8rem;cursor:pointer;font-size:.8rem}}
.refresh-btn:hover{{border-color:var(--action);color:var(--action)}}
/* ---- shell: sidebar + main ---- */
.shell{{display:flex;align-items:flex-start;min-height:calc(100vh - 50px)}}
nav{{position:sticky;top:0;align-self:flex-start;width:216px;flex-shrink:0;background:var(--nav-bg);
border-right:1px solid var(--border);padding:.7rem .55rem;display:flex;flex-direction:column;gap:.12rem;
max-height:100vh;overflow:auto}}
.nav-zone{{font-size:.66rem;font-weight:700;letter-spacing:.09em;text-transform:uppercase;
color:var(--text-3);padding:.9rem .6rem .3rem}}
.nav-zone:first-child{{padding-top:.2rem}}
.nav-b{{display:flex;align-items:center;gap:.5rem;width:100%;text-align:left;background:transparent;
color:var(--text-2);border:1px solid transparent;border-left:2px solid transparent;border-radius:6px;
padding:.46rem .6rem;font-size:.93rem;cursor:pointer;font-family:inherit}}
.nav-b:hover{{background:var(--bg-2);color:var(--text-1)}}
.nav-b.active{{background:var(--bg-2);color:var(--text-1);border-left-color:var(--action);font-weight:600}}
.nav-b .nb-ct{{margin-left:auto;font-family:ui-monospace,monospace;font-size:.72rem;color:var(--text-3);
background:var(--bg-3);border-radius:10px;padding:.02rem .42rem}}
.nav-b.active .nb-ct{{color:var(--info)}}
/* Submenús colapsables (IA de menú/submenú) */
.nav-group{{margin:.1rem 0}}
.nav-ghead{{display:flex;align-items:center;gap:.5rem;width:100%;text-align:left;background:transparent;
color:var(--text-1);border:1px solid transparent;border-radius:6px;padding:.46rem .6rem;
font-size:.93rem;font-weight:700;cursor:pointer;font-family:inherit}}
.nav-ghead:hover{{background:var(--bg-2)}}
.nav-chev{{margin-left:auto;transition:transform .15s;color:var(--text-3);font-size:.8rem}}
.nav-group.open .nav-chev{{transform:rotate(90deg)}}
.nav-gbody{{display:none;margin:.1rem 0 .3rem .55rem;padding-left:.35rem;border-left:1px solid var(--border)}}
.nav-group.open .nav-gbody{{display:block}}
main{{flex:1;max-width:1080px;padding:1.4rem 1.6rem;min-width:0}}
.sec{{display:none}} .sec.active{{display:block}}
h2{{font-size:1.3rem;margin:.2rem 0 1rem;color:var(--text-1)}} h3{{font-size:1.05rem;color:var(--text-1)}}
.lead{{font-size:1.05rem;background:var(--bg-1);border:1px solid var(--border);border-left:3px solid var(--ok);
border-radius:8px;padding:1rem 1.2rem;white-space:pre-line;color:var(--text-1)}}
.hint{{color:var(--text-2)}} .more{{margin-top:1rem}}
.more summary{{cursor:pointer;font-weight:600;color:var(--action)}}
.why li{{margin:.35rem 0}}
.sec h3{{margin:1.3rem 0 .4rem}}
.sec-ul{{padding-left:1.2rem}} .sec-ul li{{margin:.4rem 0;color:var(--text-1)}}
.kv{{display:grid;grid-template-columns:max-content 1fr;gap:.35rem .9rem;margin:.5rem 0;
background:var(--bg-1);border:1px solid var(--border);border-radius:8px;padding:.8rem 1.1rem}}
.kv dt{{font-weight:600;color:var(--text-2)}} .kv dd{{margin:0;color:var(--text-1)}}
.tbl-wrap{{overflow-x:auto;margin:.6rem 0;border-radius:8px}}
table.cmp{{border-collapse:collapse;width:100%;background:var(--bg-1);font-size:.9rem}}
table.cmp th,table.cmp td{{border:1px solid var(--border);padding:.45rem .7rem;text-align:left;
vertical-align:top;color:var(--text-1)}}
table.cmp th{{background:var(--bg-2);color:var(--text-2)}}
.cards{{display:grid;gap:.9rem}}
.card{{background:var(--bg-1);border:1px solid var(--border);border-radius:10px;padding:1rem 1.2rem}}
.card h3{{margin:0 0 .3rem}} .when{{color:var(--ok);background:#10261a;border:1px solid #1c3a25;border-radius:6px;
padding:.3rem .6rem;display:inline-block;font-size:.9rem;margin-bottom:.5rem}}
.steps li{{margin:.25rem 0}} .involved{{color:var(--text-3);font-size:.85rem;margin-top:.5rem}}
/* salud + buscador */
.health{{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:.4rem 0}}
.health-l{{color:var(--text-2);font-size:.9rem;margin-right:.3rem}}
.hb{{background:var(--bg-1);border:1px solid var(--border);color:var(--text-1);border-radius:20px;
padding:.25rem .7rem;cursor:pointer;font-size:.85rem}} .hb:hover{{border-color:var(--border-hi)}}
.hb.active{{background:var(--bg-3);border-color:var(--action)}} .hb.active b{{color:var(--info)!important}}
.search{{width:100%;padding:.6rem .9rem;font-size:1rem;background:var(--bg-3);color:var(--text-1);
border:1px solid var(--border);border-radius:9px;margin:.5rem 0 .3rem}}
.search::placeholder{{color:var(--text-3)}}
/* grupos */
.grp{{background:var(--bg-1);border:1px solid var(--border);border-radius:10px;margin:.8rem 0;overflow:hidden}}
.grp-h{{margin:0;padding:.8rem 1rem;cursor:pointer;background:var(--bg-2);font-size:1.02rem;color:var(--text-1)}}
.grp-h small{{color:var(--text-3);font-weight:400}}
.grp-where{{color:var(--ok);padding:.4rem 1rem;font-size:.9rem;background:#0e1a13}}
.grp .grp-body{{display:none}} .grp.open .grp-body{{display:block}}
.grp.open .grp-h .tog{{transform:rotate(0)}} .grp-h .tog{{display:inline-block}}
/* tarjeta de componente */
.comp{{border-top:1px solid var(--border)}}
.comp-h{{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;padding:.6rem 1rem;cursor:pointer;
border-left:3px solid var(--border-hi)}}
.comp-h:hover{{background:var(--bg-2)}}
.tog{{color:var(--text-3);font-size:.8rem;transition:transform .15s}}
.comp.open .comp-h .tog{{transform:rotate(90deg)}}
.fam-ic{{width:1.6rem;height:1.6rem;display:inline-flex;align-items:center;justify-content:center;
border-radius:6px;font-size:.95rem;flex-shrink:0}}
.comp-nm{{font-weight:700;white-space:nowrap;font-size:.95rem;color:var(--text-1)}}
.status-chip{{color:#0d1117;font-size:.72rem;font-weight:700;padding:.1rem .55rem;border-radius:20px;white-space:nowrap}}
.tested{{font-size:.78rem;white-space:nowrap}} .tested.ok{{color:var(--ok)}} .tested.no{{color:var(--warn)}}
.chip.med{{background:#2d0f0f;color:#ff9d9d;border:1px solid #5a1a1a;font-size:.72rem;
padding:.1rem .5rem;border-radius:20px}}
.enunciado{{flex-basis:100%;color:var(--text-2);font-size:.95rem;padding-left:2.1rem}}
.comp .comp-d{{display:none;padding:.4rem 1.2rem 1rem 2.2rem}}
.comp.open .comp-d{{display:block}}
.sem-lbl{{font-size:.78rem;text-transform:uppercase;letter-spacing:.04em;color:var(--text-3);font-weight:700;
margin:.2rem 0 .3rem}}
ul.semantic{{margin:.2rem 0 .7rem;padding-left:1.2rem}} ul.semantic li{{margin:.35rem 0;color:var(--text-1)}}
.sci-btn{{background:transparent;color:var(--action);border:1px solid var(--border);border-radius:8px;
padding:.45rem .9rem;font-size:.9rem;cursor:pointer;font-weight:600}}
.sci-btn:hover{{border-color:var(--action);background:var(--bg-2)}}
code{{background:var(--bg-3);color:var(--info);padding:.05rem .35rem;border-radius:4px;font-size:.85rem}}
/* modal científico */
.modal{{display:none;position:fixed;inset:0;background:rgba(1,4,9,.7);z-index:50;
padding:2rem 1rem;overflow:auto}} .modal.show{{display:block}}
.modal-box{{background:var(--bg-1);border:1px solid var(--border);max-width:760px;margin:auto;
border-radius:12px;padding:1.2rem 1.5rem;box-shadow:0 12px 40px rgba(0,0,0,.5)}}
.modal-box h3{{margin:.1rem 0 .6rem}} .modal-box h4{{margin:1rem 0 .3rem;color:var(--hook)}}
.modal-box ul{{padding-left:1.2rem}} .modal-box li{{margin:.3rem 0;color:var(--text-1)}}
.modal-x{{float:right;background:var(--bg-3);color:var(--text-1);border:1px solid var(--border);
border-radius:7px;padding:.35rem .8rem;cursor:pointer;font-size:.9rem}} .modal-x:hover{{border-color:var(--border-hi)}}
.code-btn{{background:var(--action);color:#0d1117;border:0;border-radius:8px;padding:.45rem .9rem;
cursor:pointer;font-size:.88rem;margin:.6rem 0 0;font-weight:600}} .code-btn:hover{{filter:brightness(1.08)}}
.codeview{{background:#010409;color:var(--text-1);padding:.8rem 1rem;border-radius:8px;overflow:auto;
font:12.5px/1.55 ui-monospace,SFMono-Regular,monospace;white-space:pre;margin-top:.6rem;tab-size:4}}
textarea.codeview{{width:100%;height:52vh;resize:vertical;border:1px solid var(--border);display:block}}
.code-area{{margin-top:.6rem}}
.code-tools{{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center;margin-bottom:.2rem}}
.ct{{background:var(--bg-2);color:var(--text-1);border:1px solid var(--border);border-radius:7px;
padding:.4rem .75rem;cursor:pointer;font-size:.85rem;font-weight:600}} .ct:hover{{border-color:var(--border-hi)}}
.ct.apply{{background:#10261a;color:var(--ok);border-color:#1c3a25}}
.ct.rev{{background:#2d0f0f;color:var(--danger);border-color:#5a1a1a;margin-left:1.5rem}}
.code-status{{color:var(--text-3);font-size:.82rem}}
.code-report h4{{margin:.6rem 0 .2rem;color:var(--hook)}}
.code-report .rep{{background:#010409;color:var(--text-1);padding:.6rem .8rem;border-radius:8px;overflow:auto;
max-height:30vh;font:12px/1.5 ui-monospace,monospace;white-space:pre-wrap}}
.code-report .cw{{background:#2a2410;color:var(--warn);padding:.5rem .8rem;border-radius:8px;margin:.4rem 0}}
.code-report .co{{background:#10261a;color:var(--ok);padding:.5rem .8rem;border-radius:8px;margin:.4rem 0}}
.struct{{background:var(--bg-2);border:1px solid var(--border);border-radius:8px;padding:.6rem .8rem;margin-top:.5rem}}
.cls,.fns{{margin:.25rem 0}} .meth{{color:var(--text-3);margin-left:.5rem;font-size:.85rem}}
.drift li{{margin:.35rem 0}} ul.drift{{background:var(--bg-1);border:1px solid var(--border);border-radius:10px;
padding:1rem 1.4rem}} ul.drift .chip{{color:#0d1117;font-size:.7rem;padding:.05rem .45rem;border-radius:20px}}
.term-bar{{display:flex;gap:.4rem;flex-wrap:wrap;margin:.6rem 0}}
.tbtn{{background:var(--bg-2);color:var(--text-1);border:1px solid var(--border);border-radius:8px;
padding:.5rem .95rem;cursor:pointer;font-size:.9rem;font-weight:600}}
.tbtn:hover{{border-color:var(--border-hi)}} .tbtn.alt{{background:#2d0f0f;color:var(--danger);border-color:#5a1a1a;margin-left:1.5rem}}
#termwrap{{background:#010409;border:1px solid var(--border);border-radius:10px;padding:.7rem;height:480px}} #xterm{{height:100%}}
/* conductas vivas (memoria/pulso/hooks/mcp) */
.live-offline{{background:#2a2410;color:var(--warn);border:1px solid #4a3f10;padding:.6rem .9rem;border-radius:8px;margin:.5rem 0}}
.live-cards{{display:flex;flex-wrap:wrap;gap:.7rem;margin:.6rem 0}}
.lc{{background:var(--bg-1);border:1px solid var(--border);border-radius:10px;padding:.6rem 1rem;min-width:7rem}}
.lc b{{font-size:1.5rem;display:block;color:var(--text-1);font-family:ui-monospace,monospace}}
.lc span{{color:var(--text-2);font-size:.82rem}}
.mem-tbl{{width:100%;border-collapse:collapse;background:var(--bg-1);border:1px solid var(--border);
border-radius:8px;overflow:hidden;font-size:.9rem;margin:.5rem 0}}
.mem-tbl th,.mem-tbl td{{text-align:left;padding:.45rem .7rem;border-bottom:1px solid var(--border);color:var(--text-1)}}
.mem-tbl th{{background:var(--bg-2);color:var(--text-2)}} .mem-tbl td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.mlist{{list-style:none;padding:0;margin:.4rem 0}}
.mlist li{{background:var(--bg-1);border:1px solid var(--border);border-radius:8px;padding:.5rem .8rem;margin:.35rem 0;color:var(--text-1)}}
.mlist .tag{{font-size:.72rem;background:var(--bg-3);color:var(--text-2);border-radius:20px;padding:.05rem .5rem;margin-right:.4rem}}
.mlist .lock{{color:var(--warn)}}
.mem-filters{{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin:.5rem 0}}
.mem-qbox{{flex:1;min-width:13rem;margin:0}}
.mem-sel{{background:var(--bg-3);color:var(--text-1);border:1px solid var(--border);border-radius:7px;
padding:.45rem .6rem;font-size:.88rem;max-width:13rem}}
.hb.on{{background:var(--bg-3);border-color:var(--action)}} .hb.on{{color:var(--info)}}
.mem-result{{background:var(--bg-1);border:1px solid var(--border);border-left:3px solid var(--border-hi);
border-radius:8px;padding:.5rem .8rem;margin:.35rem 0;color:var(--text-1)}}
.mem-result.locked{{border-left-color:var(--warn)}} .mem-result.guard{{border-left-color:var(--hook)}}
.mem-result .mr-meta{{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center;margin-bottom:.25rem}}
.mem-result .mr-when{{margin-left:auto;color:var(--text-3);font-family:ui-monospace,monospace;font-size:.74rem}}
.mem-result,.mlist li,.lead,.feed .ev .s{{overflow-wrap:anywhere;word-break:break-word}}
code{{overflow-wrap:anywhere}}
/* ---- A0: tablero de estado (semáforos) ---- */
.st-key{{font-weight:700;padding:.02rem .4rem;border-radius:5px}}
.st-key.ok{{background:#10261a;color:var(--ok)}} .st-key.warn{{background:#2a2410;color:var(--warn)}}
.st-key.down{{background:#2d0f0f;color:var(--danger)}}
.st-banner{{display:flex;align-items:center;gap:.7rem;font-size:1.05rem;font-weight:700;padding:.7rem 1rem;
border-radius:10px;margin:.4rem 0 .8rem;border:1px solid var(--border)}}
.st-banner.ok{{background:#10261a;color:var(--ok)}} .st-banner.warn{{background:#2a2410;color:var(--warn)}}
.st-banner.down{{background:#2d0f0f;color:var(--danger)}}
.st-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(16rem,1fr));gap:.6rem;margin:.6rem 0}}
.st-item{{background:var(--bg-1);border:1px solid var(--border);border-left:4px solid var(--text-3);
border-radius:9px;padding:.6rem .8rem;min-width:0}}
.st-item.ok{{border-left-color:var(--ok)}} .st-item.warn{{border-left-color:var(--warn)}}
.st-item.down{{border-left-color:var(--danger)}}
.st-name{{font-weight:700;display:flex;align-items:center;gap:.45rem;color:var(--text-1)}}
.st-dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}
.st-dot.ok{{background:var(--ok)}} .st-dot.warn{{background:var(--warn)}} .st-dot.down{{background:var(--danger)}}
.st-fchip{{cursor:pointer;padding:1px 7px;border-radius:7px;transition:background .12s}}
.st-fchip:hover{{background:rgba(255,255,255,.08)}} .st-fchip.active{{background:rgba(255,255,255,.16);font-weight:600}}
.atom-tabs{{display:flex;flex-wrap:wrap;gap:.35rem;margin:.2rem 0 1rem;border-bottom:1px solid var(--border);padding-bottom:.6rem}}
.atab{{cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--text-2);padding:.35rem .8rem;border-radius:8px;font-size:.9rem;font-weight:600}}
.atab:hover{{border-color:var(--border-hi);color:var(--text)}}
.atab.active{{background:var(--accent,#1f6feb);color:#fff;border-color:transparent}}
.cap-actions{{display:flex;align-items:center;gap:.6rem;margin:.2rem 0 .8rem}}
.cap-list{{display:flex;flex-direction:column;gap:.3rem;margin:.3rem 0 1.1rem}}
.cap-row{{display:grid;grid-template-columns:7rem 12rem 6.5rem auto 1.6rem;gap:.6rem;align-items:baseline;
  padding:.45rem .6rem;border:1px solid var(--border);border-left:3px solid var(--border);border-radius:7px;background:var(--bg-1)}}
.cap-row:hover{{border-color:var(--border-hi)}}
.cap-verdict{{font-weight:600;font-size:.82rem;white-space:nowrap}}
.cap-name{{font-family:ui-monospace,monospace;font-size:.85rem;color:var(--text-1);font-weight:600;overflow-wrap:anywhere}}
.cap-uso{{font-size:.78rem;color:var(--text-2);white-space:nowrap;line-height:1.25}}
.cap-desc{{font-size:.8rem;color:var(--text-2)}}
.cap-cuando{{font-size:.76rem;color:var(--text-3);margin-top:.2rem}}
.cap-test{{font-size:.95rem;text-align:center}}
.cap-flag{{font-size:.68rem;color:#9a6700;background:#fff3cd;border-radius:4px;padding:0 .35rem}}
.cap-usar{{border-left-color:#1a7f37}} .cap-promover,.cap-revisar{{border-left-color:#bf8700}}
.cap-ocioso,.cap-inactivo{{border-left-color:#cf222e}} .cap-activo{{border-left-color:#57606a}}
@media(max-width:760px){{.cap-row{{grid-template-columns:1fr}}}}
.afgroup{{border:1px solid var(--border);border-radius:8px;margin-bottom:.5rem;padding:.5rem .8rem}}
.afgroup summary{{cursor:pointer;font-size:.95rem}} .afbody{{margin-top:.5rem}}
.afrow{{display:flex;gap:.6rem;align-items:center;padding:.25rem 0;border-top:1px solid var(--border);font-size:.85rem}}
.uip-flow{{display:flex;flex-wrap:wrap;gap:.7rem;margin:.4rem 0}}
.uip-phase{{flex:1;min-width:240px;border:1px solid var(--border);border-radius:10px;padding:.8rem}}
.uip-ph{{font-weight:800;letter-spacing:.04em;color:var(--accent,#1f6feb)}} .uip-tool{{font-family:ui-monospace,monospace;font-size:.82rem;color:var(--text-2);margin:.2rem 0 .4rem}}
.uip-desc{{font-size:.86rem;color:var(--text-2)}}
.brief-card{{border:1px solid var(--border);border-radius:8px;padding:.6rem .8rem;margin-bottom:.5rem}}
.brief-h{{display:flex;justify-content:space-between;align-items:center;margin-bottom:.3rem}}
.brief-l{{font-size:.85rem;color:var(--text-2);margin:.15rem 0}}
.qrow{{display:grid;grid-template-columns:1fr;gap:.25rem;padding:.5rem .7rem;border:1px solid var(--border);border-radius:8px;margin-bottom:.4rem}}
.qname{{display:flex;align-items:center;gap:.5rem;font-family:ui-monospace,monospace;font-size:.88rem;color:var(--text-1)}}
.qbar{{height:7px;background:var(--bg-3);border-radius:5px;overflow:hidden}} .qbarfill{{height:100%;border-radius:5px;transition:width .3s}}
.qmeta{{font-size:.78rem;color:var(--text-2)}}
.qbadge{{font-size:.68rem;font-weight:700;padding:.05rem .45rem;border-radius:6px;letter-spacing:.02em}}
.q-never{{background:rgba(248,81,73,.18);color:var(--danger)}} .q-dirty{{background:rgba(210,153,34,.18);color:var(--warn)}} .q-ok{{background:rgba(63,185,80,.16);color:var(--ok)}}
.st-item{{cursor:pointer}} .st-item:hover{{border-color:var(--border-hi)}}
.st-metric{{font-family:ui-monospace,monospace;font-size:1.15rem;font-weight:700;color:var(--text-1);margin:.3rem 0 .25rem}}
.st-bar{{height:.5rem;background:var(--bg-3);border-radius:20px;overflow:hidden;margin:.1rem 0 .4rem}}
.st-fill{{height:100%;border-radius:20px;transition:width .3s}}
.st-fill.ok{{background:var(--ok)}} .st-fill.warn{{background:var(--warn)}} .st-fill.down{{background:var(--danger)}}
.st-purpose{{color:var(--text-2);font-size:.84rem;overflow-wrap:anywhere}}
/* filas del modal de estado */
.st-mfilter{{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:.3rem 0 .8rem}}
.st-row{{border:1px solid var(--border);border-left:4px solid var(--text-3);border-radius:8px;
padding:.5rem .8rem;margin:.4rem 0}}
.st-row.ok{{border-left-color:var(--ok)}} .st-row.warn{{border-left-color:var(--warn)}}
.st-row.down{{border-left-color:var(--danger)}}
.st-rowmet{{margin-left:auto;font-family:ui-monospace,monospace;font-size:.82rem;color:var(--text-2)}}
.st-row{{cursor:pointer}} .st-row:hover{{border-color:var(--border-hi)}}
.st-metric.st-ok{{color:var(--ok)}} .st-metric.st-warn{{color:var(--warn)}} .st-metric.st-down{{color:var(--danger)}}
.modal-box.wide{{max-width:1060px}}
.modal-box,#modal-content{{min-width:0}} #modal-content p,#modal-content h4{{overflow-wrap:anywhere}}
/* esqueleto/código en el modal: envuelve y hace scroll DENTRO del box (no se sale) */
.rep{{background:#010409;color:var(--text-1);border:1px solid var(--border);border-radius:8px;
padding:.6rem .8rem;margin:.5rem 0;overflow:auto;max-width:100%;max-height:42vh;
white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;
font:12px/1.5 ui-monospace,SFMono-Regular,monospace}}
.skel-pre{{background:#010409;color:var(--text-1);border:1px solid var(--border);border-radius:8px;
padding:.6rem .8rem;margin:.5rem 0;overflow:auto;max-width:100%;max-height:38vh;
white-space:pre;overflow-wrap:normal;
font:12px/1.5 ui-monospace,SFMono-Regular,monospace;tab-size:2}}
.atom-card{{background:var(--bg-1);border:1px solid var(--border);border-radius:9px;padding:.55rem .85rem;margin:.4rem 0;cursor:pointer}}
.atom-card:hover{{border-color:var(--action)}}
.atom-h{{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap}}
.atom-nm{{font-family:ui-monospace,monospace;font-weight:700;color:var(--text-1);font-size:.93rem;overflow-wrap:anywhere}}
.atom-proj{{margin-left:auto;font-size:.72rem;background:var(--bg-3);color:var(--info);border-radius:10px;padding:.05rem .5rem;white-space:nowrap}}
.atom-meta{{color:var(--text-2);font-size:.82rem;margin-top:.2rem}}
.afilter-row{{display:flex;flex-wrap:wrap;gap:.4rem;align-items:center;margin:.25rem 0;padding:.25rem 0;border-bottom:1px solid var(--border)}}
.afilter-row:last-child{{border-bottom:0}}
.afilter-lbl{{color:var(--text-3);font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;min-width:6.5rem}}
.hb-tr{{color:var(--info)}} .hb-tr.active{{background:var(--bg-3);border-color:var(--info)}}
.atom-val{{font-size:.7rem;font-weight:700;border-radius:10px;padding:.05rem .5rem;background:var(--bg-3);color:var(--text-2);white-space:nowrap}}
.atom-val.st-ok{{color:var(--ok)}} .atom-val.st-warn{{color:var(--warn)}} .atom-val.st-down{{color:var(--danger)}}
.rice-bar{{height:4px;background:var(--bg-3);border-radius:2px;margin-top:.35rem;overflow:hidden}}
.rice-fill{{height:100%;background:var(--action);border-radius:2px;transition:width .3s ease}}
.audit-card{{background:var(--bg-1);border:1px solid var(--border);border-radius:9px;
padding:.65rem .9rem;margin:.5rem 0}}
.audit-h{{display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-bottom:.3rem}}
.audit-type{{font-family:ui-monospace,monospace;font-weight:700;color:var(--text-1);font-size:.88rem}}
.audit-cnt{{background:var(--bg-3);color:var(--info);border-radius:10px;padding:.05rem .5rem;
font-size:.75rem;font-weight:700;white-space:nowrap}}
.audit-desc{{color:var(--text-2);font-size:.82rem}}
.st-date{{margin-left:auto;font-family:ui-monospace,monospace;font-size:.74rem;color:var(--text-3);white-space:nowrap}}
.st-name{{width:100%}}
.proj-row{{cursor:pointer}} .proj-row:hover td{{background:var(--bg-2)}}
.proj-about{{margin:.5rem 0;padding:.6rem .9rem;background:var(--bg-2);border:1px solid var(--border);
border-radius:8px;color:var(--text-1);min-height:1.2rem}} .proj-about:empty{{display:none}}
/* ---- sidebar colapsable ---- */
.nav-toggle{{background:var(--bg-2);color:var(--text-1);border:1px solid var(--border);border-radius:7px;
padding:.3rem .6rem;cursor:pointer;font-size:1rem;line-height:1}}
body.nav-collapsed nav{{width:0;padding-left:0;padding-right:0;overflow:hidden;border-right:0}}
body.nav-collapsed .nav-b,body.nav-collapsed .nav-zone{{display:none}}
.feed{{background:#010409;border:1px solid var(--border);border-radius:10px;padding:.6rem .8rem;max-height:56vh;overflow:auto;
font:12.5px/1.5 ui-monospace,monospace}}
.feed .ev{{display:flex;gap:.6rem;padding:.15rem 0;border-bottom:1px solid var(--bg-2);color:var(--text-1)}}
.feed .ev .t{{color:var(--text-3);white-space:nowrap}} .feed .ev .ty{{color:var(--ok);white-space:nowrap;font-weight:600}}
.feed .ev .s{{color:var(--text-2)}} .feed .ev.new{{background:#10261a;animation:slide-in 150ms ease-out}}
/* 📥 Intake */
.proj-intk-form{{margin:.5rem 0 1rem}}
.intk-ta{{display:block;width:100%;min-height:7rem;padding:.5rem .7rem;background:var(--bg-3);
color:var(--text-1);border:1px solid var(--border);border-radius:7px;font-size:.9rem;
resize:vertical;margin-top:.3rem}} .intk-ta::placeholder{{color:var(--text-3)}}
/* 🔨 Proyecto / Cowork */
.proj-commit{{background:var(--bg-1);border:1px solid var(--border);border-radius:10px;padding:.7rem 1rem;margin:.5rem 0}}
.proj-sha{{display:flex;align-items:baseline;gap:.6rem;flex-wrap:wrap}}
.proj-sha-badge{{font:12px ui-monospace,monospace;background:var(--bg-2);color:var(--ok);border-radius:6px;padding:.1rem .45rem}}
.proj-meta{{font-size:.82rem;margin:.2rem 0}}
.proj-files{{font-size:.82rem;margin:.2rem 0;color:var(--text-2)}}
.proj-why{{background:var(--bg-2);border-left:3px solid var(--action);border-radius:0 6px 6px 0;padding:.35rem .7rem;margin:.35rem 0;font-size:.84rem;color:var(--text-2)}}
.proj-tag{{display:inline-block;font:10px/1.4 ui-monospace,monospace;border-radius:4px;padding:0 .35rem;font-weight:700;margin-right:.3rem}}
.proj-dec{{background:#10261a;color:var(--ok)}} .proj-dig{{background:#0d1b2a;color:var(--info)}} .proj-gate{{background:#2a2410;color:var(--warn)}}
.proj-comments{{margin:.4rem 0;padding:.3rem .5rem;background:var(--bg-0);border-radius:6px;min-height:1.2rem;font-size:.85rem}}
.proj-cm{{display:flex;gap:.4rem;flex-wrap:wrap;padding:.1rem 0;border-bottom:1px solid var(--bg-2)}}
.proj-cm-author{{color:var(--info);font-weight:600;white-space:nowrap}}
.proj-cm-body{{color:var(--text-1);overflow-wrap:anywhere}}
.proj-cform{{display:flex;gap:.3rem;align-items:center;flex-wrap:wrap;margin-top:.4rem}}
.proj-in-progress{{border:2px solid var(--warn);border-radius:10px;padding:.7rem 1rem;margin:.5rem 0 1rem;background:#1f1800;animation:slide-in .25s ease}}
.proj-build-header{{display:flex;align-items:center;gap:.5rem;margin-bottom:.5rem;flex-wrap:wrap}}
.proj-build-badge{{font:11px/1.4 ui-monospace,monospace;font-weight:700;background:var(--warn);color:#000;border-radius:5px;padding:.1rem .45rem;letter-spacing:.04em}}
.proj-build-run{{margin:.4rem 0;padding:.4rem .6rem;background:var(--bg-0);border-radius:6px}}
.proj-build-repo{{font:12px ui-monospace,monospace;color:var(--text-2);word-break:break-all}}
.proj-log{{font:11px/1.5 ui-monospace,monospace;background:#0d0d0d;color:#c8f5a0;border-radius:6px;padding:.5rem .7rem;margin:.35rem 0;max-height:200px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}}
@keyframes slide-in{{from{{opacity:0;transform:translateY(-4px)}}to{{opacity:1;transform:translateY(0)}}}}
.pulse-bar{{display:flex;gap:.4rem;align-items:center;margin:.5rem 0}}
.hk-tbl{{width:100%;border-collapse:collapse;background:var(--bg-1);border:1px solid var(--border);border-radius:8px;
overflow:hidden;font-size:.9rem;margin:.5rem 0}} .hk-tbl th,.hk-tbl td{{padding:.45rem .7rem;
border-bottom:1px solid var(--border);text-align:left;vertical-align:top;color:var(--text-1)}} .hk-tbl th{{background:var(--bg-2);color:var(--text-2)}}
.hk-tbl code{{font-size:.78rem}} .hk-yes{{color:var(--ok);font-weight:700}} .hk-no{{color:var(--text-3)}}
.hk-sub td{{background:var(--bg-2);font-size:.82rem;opacity:.85}}
.hk-uncabled td{{opacity:.6;font-style:italic}}
.mcp-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(15rem,1fr));gap:.8rem;margin:.6rem 0}}
.mcp-tool{{background:var(--bg-1);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem}}
.mcp-tool h3{{margin:0 0 .4rem;font-family:ui-monospace,monospace;font-size:.95rem}}
.mcp-in{{width:100%;padding:.4rem .6rem;background:var(--bg-3);color:var(--text-1);border:1px solid var(--border);
border-radius:7px;margin:.2rem 0;font-size:.88rem}} .mcp-in::placeholder{{color:var(--text-3)}}
.kind{{font-size:.66rem;font-weight:700;padding:.05rem .45rem;border-radius:20px;vertical-align:middle}}
.kind.read{{background:#10261a;color:var(--ok)}} .kind.local{{background:#1a1033;color:var(--mcp)}}
.kind.write{{background:#2d1a0f;color:var(--hook)}}
.mcp-out{{background:#010409;color:var(--text-1);border:1px solid var(--border);border-radius:8px;padding:.8rem 1rem;margin-top:.6rem;
font:12.5px/1.55 ui-monospace,monospace;white-space:pre-wrap;max-height:46vh;overflow:auto;display:none}}
.mcp-out.show{{display:block}}
/* cabina del amplificador */
.body-badge{{font-weight:700;padding:.1rem .6rem;border-radius:20px;font-size:.8rem}}
.body-up{{background:#10261a;color:var(--ok)}} .body-down{{background:var(--bg-3);color:var(--text-2)}}
.prog-wrap{{background:var(--bg-1);border:1px solid var(--border);border-radius:10px;padding:.7rem 1rem;margin:.6rem 0;color:var(--text-1)}}
.prog-bar{{height:.7rem;background:var(--bg-3);border-radius:20px;overflow:hidden;margin:.4rem 0}}
.prog-fill{{height:100%;background:var(--ok);border-radius:20px;transition:width .3s}}
.amp-row{{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap;background:var(--bg-1);border:1px solid var(--border);
border-radius:8px;padding:.5rem .8rem;margin:.35rem 0}}
.amp-row .meta{{color:var(--text-3);font-size:.82rem;font-family:ui-monospace,monospace}}
.amp-row .tool{{font-weight:700;font-family:ui-monospace,monospace;color:var(--text-1)}}
.amp-row .sp{{flex:1}}
.lbl-btn{{border:1px solid var(--border);border-radius:7px;padding:.35rem .8rem;cursor:pointer;font-weight:600;font-size:.85rem}}
.lbl-ok{{background:#10261a;color:var(--ok);border-color:#1c3a25}} .lbl-ok:hover{{filter:brightness(1.2)}}
.lbl-no{{background:#2d0f0f;color:var(--danger);border-color:#5a1a1a}} .lbl-no:hover{{filter:brightness(1.2)}}
@media (max-width:760px){{.shell{{flex-direction:column}} nav{{width:100%;flex-direction:row;flex-wrap:wrap;
position:static;border-right:0;border-bottom:1px solid var(--border)}} .nav-zone{{width:100%;padding:.4rem .6rem .1rem}}}}
@media (prefers-reduced-motion:reduce){{.live-dot,.feed .ev.new{{animation:none}} *{{transition-duration:.01ms!important}}}}
</style>
<link rel="stylesheet" href="/vendor/xterm.css">
<script src="/vendor/xterm.js"></script><script src="/vendor/xterm-fit.js"></script>
</head><body>
<header>
<button class="nav-toggle" onclick="toggleNav()" title="ocultar / mostrar menú">☰</button>
<h1><span class="live-dot" title="consola viva"></span> ARIS4U</h1>
<div class="statusstrip">
<span><b>{branch}</b>@{head}</span><span class="sep">|</span>
<span><b>{n}</b> piezas</span><span class="sep">|</span>
<span class="timestamp" title="generado del código vivo">{gen}</span>
</div>
<button class="refresh-btn" onclick="refreshConsole(this)" title="releer el código">⟳ Refrescar</button>
</header>
<div class="shell">
<nav>{nav}</nav>
<main>{body}</main>
</div>
<div id="modal" class="modal"><div class="modal-box" id="modal-box">
<button class="modal-x" onclick="closeModal()">✕ cerrar</button><div id="modal-content"></div></div></div>
<script>
document.querySelectorAll('.nav-b').forEach(function(b){{
  b.addEventListener('click',function(){{
    document.querySelectorAll('.nav-b').forEach(function(x){{x.classList.remove('active');}});
    document.querySelectorAll('.sec').forEach(function(x){{x.classList.remove('active');}});
    b.classList.add('active');
    var s=document.getElementById(b.dataset.s); if(s) s.classList.add('active');
    if(b.dataset.s==='term' && window._fitTerm) window._fitTerm();
    window.scrollTo(0,0);
  }});
}});
function refreshConsole(b){{var t=b.textContent;b.textContent='regenerando…';
  fetch('/regenerate',{{method:'POST'}}).then(function(){{location.reload();}})
  .catch(function(){{b.textContent='🔄 (necesita el servidor)';setTimeout(function(){{b.textContent=t;}},2000);}});}}
var _term=null,_fit=null,_es=null,_sid=null;
function _b64(str){{var b=new TextEncoder().encode(str),s='';b.forEach(function(c){{s+=String.fromCharCode(c);}});return btoa(s);}}
function _sendResize(){{if(_sid&&_term)fetch('/pty/resize',{{method:'POST',body:JSON.stringify({{id:_sid,rows:_term.rows,cols:_term.cols}})}});}}
window._fitTerm=function(){{if(_term&&_fit){{_fit.fit();_sendResize();_term.focus();}}}};
function _ensureTerm(){{
  if(typeof Terminal==='undefined'){{document.getElementById('term-note').textContent='Las terminales necesitan el servidor: python3 -m aris4u_console.server';return false;}}
  if(!_term){{
    _term=new Terminal({{fontSize:13,fontFamily:'ui-monospace,monospace',cursorBlink:true,theme:{{background:'#0d1117',foreground:'#c9d1d9'}}}});
    _fit=new FitAddon.FitAddon();_term.loadAddon(_fit);
    _term.open(document.getElementById('xterm'));_fit.fit();
    _term.onData(function(d){{if(_sid)fetch('/pty/input',{{method:'POST',body:JSON.stringify({{id:_sid,data:_b64(d)}})}});}});
    window.addEventListener('resize',function(){{if(_term){{_fit.fit();_sendResize();}}}});
  }}
  return true;
}}
function startTerm(preset){{
  if(!_ensureTerm())return;
  killTerm();
  document.getElementById('term-note').textContent='';
  _term.clear();_fit.fit();_term.focus();
  fetch('/pty/start',{{method:'POST',body:JSON.stringify({{preset:preset}})}}).then(function(r){{return r.json();}}).then(function(d){{
    _sid=d.id;_sendResize();
    _es=new EventSource('/pty/stream?id='+_sid);
    _es.onmessage=function(e){{var bin=atob(e.data),arr=new Uint8Array(bin.length);for(var i=0;i<bin.length;i++){{arr[i]=bin.charCodeAt(i);}}_term.write(arr);}};
    _es.addEventListener('exit',function(){{_term.write('\\r\\n[sesión terminada]\\r\\n');if(_es){{_es.close();_es=null;}}_sid=null;}});
  }});
}}
function killTerm(){{if(_es){{_es.close();_es=null;}}_sid=null;}}
function openModal(e,id){{ e.stopPropagation(); var s=document.getElementById(id); if(!s)return;
  document.getElementById('modal-content').innerHTML=s.innerHTML;
  document.getElementById('modal-box').classList.remove('wide');
  document.getElementById('modal').classList.add('show'); }}
function closeModal(){{ document.getElementById('modal').classList.remove('show');
  document.getElementById('modal-box').classList.remove('wide'); }}
function loadCode(path, btn){{
  var area=btn.nextElementSibling; area.hidden=false;
  var ta=area.querySelector('.codeview'); ta.value='cargando…';
  fetch('/code?path='+encodeURIComponent(path)).then(function(r){{return r.ok?r.json():Promise.reject(r.status);}})
    .then(function(d){{ta.value=d.content;ta.dataset.hash=d.hash;}})
    .catch(function(e){{ta.value=(e===403?'Ruta no permitida.':'Código no disponible en modo archivo. Abre con el servidor: python3 -m aris4u_console.server');}});
}}
function _cctx(btn){{var a=btn.closest('.code-area');return {{ta:a.querySelector('.codeview'),report:a.querySelector('.code-report'),status:a.querySelector('.code-status')}};}}
function _esc(s){{var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}}
function reviewCode(btn){{
  var c=_cctx(btn); c.status.textContent='revisando…';
  fetch('/review',{{method:'POST',body:JSON.stringify({{path:c.ta.dataset.path,content:c.ta.value,base_hash:c.ta.dataset.hash}})}})
   .then(function(r){{return r.json();}}).then(function(d){{c.status.textContent='';
     var h=''; if(d.stale)h+='<div class="cw">⚠️ El archivo cambió en disco; recarga antes de aplicar.</div>';
     h+='<h4>Guards (ruff)</h4><pre class="rep">'+_esc(d.lint)+'</pre>';
     h+='<h4>🧠 Crítica del modelo local</h4><pre class="rep">'+_esc(d.critique)+'</pre>';
     c.report.innerHTML=h;
   }}).catch(function(){{c.status.textContent='';c.report.textContent='Revisión no disponible (¿servidor?).';}});
}}
function applyCode(btn){{
  if(!confirm('¿Aplicar el cambio al archivo real? Se puede revertir.'))return;
  var c=_cctx(btn); c.status.textContent='aplicando + corriendo tests…';
  fetch('/apply',{{method:'POST',body:JSON.stringify({{path:c.ta.dataset.path,content:c.ta.value,base_hash:c.ta.dataset.hash}})}})
   .then(function(r){{return r.json();}}).then(function(d){{c.status.textContent='';
     if(d.stale){{c.report.innerHTML='<div class="cw">⚠️ El archivo cambió en disco; recarga.</div>';return;}}
     c.ta.dataset.hash=d.hash; var t=d.test||{{}};
     c.report.innerHTML='<div class="'+(t.ok?'co':'cw')+'">'+(t.ok?'✅ Aplicado · tests pasaron':'❌ Aplicado · TESTS FALLARON (considera revertir)')+'</div><pre class="rep">'+_esc(t.summary||'')+'</pre>';
   }}).catch(function(){{c.status.textContent='';c.report.textContent='No se pudo aplicar.';}});
}}
function revertCode(btn){{
  var c=_cctx(btn); c.status.textContent='revirtiendo…';
  fetch('/revert',{{method:'POST',body:JSON.stringify({{path:c.ta.dataset.path}})}})
   .then(function(r){{return r.json();}}).then(function(d){{c.status.textContent='';c.ta.value=d.content;c.ta.dataset.hash=d.hash;c.report.innerHTML='<div class="co">↩︎ Revertido a la versión de git.</div>';}})
   .catch(function(){{c.status.textContent='';c.report.textContent='No se pudo revertir.';}});
}}
document.getElementById('modal').addEventListener('click',function(e){{ if(e.target.id==='modal') closeModal(); }});
document.addEventListener('keydown',function(e){{ if(e.key==='Escape') closeModal(); }});
function applyFilters(){{
  var box=document.getElementById('search'); var q=(box?box.value:'').toLowerCase().trim();
  var af=document.querySelector('.hb.active'); var f=af?af.dataset.f:'all';
  document.querySelectorAll('#inv .comp').forEach(function(c){{
    var okF=(f==='all')||c.dataset.status===f;
    var en=c.querySelector('.enunciado'); var et=en?en.textContent.toLowerCase():'';
    var okS=!q||c.dataset.name.indexOf(q)>=0||et.indexOf(q)>=0;
    c.style.display=(okF&&okS)?'':'none';
  }});
}}
function filterComps(){{ applyFilters(); }}
document.querySelectorAll('.hb').forEach(function(b){{ b.addEventListener('click',function(){{
  document.querySelectorAll('.hb').forEach(function(x){{x.classList.remove('active');}});
  b.classList.add('active'); applyFilters(); }}); }});
/* ---- Track A: conductas vivas (memoria / pulso / hooks / mcp) ---- */
function _esc2(s){{var d=document.createElement('div');d.textContent=(s==null?'':s);return d.innerHTML;}}
function _card(n,label,raw){{return '<div class=lc><b>'+(n==null?'—':n)+'</b><span>'+(raw?label:_esc2(label))+'</span></div>';}}
function _markOffline(sec,ok){{var el=document.querySelector('#'+sec+' .live-offline'); if(el) el.hidden=ok;}}
function onSectionShow(s){{
  if(s!=='pulso') stopPulse();
  if(s!=='proyecto') stopProyectoStream();
  if(s==='estado') loadStatus();
  else if(s==='atomos') loadAtoms();
  else if(s==='atomos-func') loadAtomFunc();
  else if(s==='valoriz') loadValorizacion();
  else if(s==='auditoria') loadAuditoria();
  else if(s==='backlog') loadBacklog();
  else if(s==='plantillas') loadSkeletons();
  else if(s==='memoria') loadMemory();
  else if(s==='pulso') loadTelemetry();
  else if(s==='hooks') {{ loadHooks(); loadPhiGuardBlocks(); }}
  else if(s==='routing') loadRouting();
  else if(s==='amp') loadAmplifier();
  else if(s==='calidad') loadQuality();
  else if(s==='briefs') loadBriefs();
  else if(s==='proyecto') loadProyecto();
  else if(s==='config') loadConfig();
  else if(s==='api') loadApi();
  else if(s==='cap-skills') loadCapSkills();
  else if(s==='cap-agents') loadCapAgents();
  else if(s==='cap-mcp') loadCapMcp();
  else if(s==='cap-api') loadCapApi();
}}
function toggleNav(){{ document.body.classList.toggle('nav-collapsed'); }}
function toggleNavGroup(h){{ h.parentNode.classList.toggle('open'); }}
/* 🧬 Átomos: barra de tabs unificada (Catálogo·Función·Proyecto·Valor·Plantillas·Calidad) */
function goAtomTab(sid){{
  document.querySelectorAll('.sec').forEach(function(x){{x.classList.remove('active');}});
  var s=document.getElementById(sid); if(s) s.classList.add('active');
  document.querySelectorAll('.atab').forEach(function(x){{x.classList.toggle('active', x.dataset.s===sid);}});
  document.querySelectorAll('.nav-b').forEach(function(x){{x.classList.toggle('active', x.dataset.s==='atomos');}});
  onSectionShow(sid);
  window.scrollTo(0,0);
}}
function goCapTab(sid){{
  document.querySelectorAll('.sec').forEach(function(x){{x.classList.remove('active');}});
  var s=document.getElementById(sid); if(s) s.classList.add('active');
  document.querySelectorAll('.atab').forEach(function(x){{x.classList.toggle('active', x.dataset.s===sid);}});
  document.querySelectorAll('.nav-b').forEach(function(x){{x.classList.toggle('active', x.dataset.s==='cap-skills');}});
  onSectionShow(sid);
  window.scrollTo(0,0);
}}
function loadCap(cat, sec, listId){{
  fetch('/cap/'+cat).then(function(r){{return r.json();}}).then(function(d){{
    _markOffline(sec, d.available!==false);
    if(d.available===false){{
      document.getElementById(listId).innerHTML='<p class=hint>'+_esc2(d.reason||'no disponible')+'</p>';
      return;
    }}
    var s=d.summary||{{}};
    document.getElementById(listId+'-sum').innerHTML =
      _card(s.total,'total',1)+_card(s.usar,'✅ usar',1)+_card(s.promover,'🟡 promover',1)
      +_card(s.revisar,'🟡 revisar',1)+_card(s.ocioso,'🔴 ocioso',1)
      +_card((s.activo||0)+(s.inactivo||0),'⚪ otros',1);
    var h='';
    (d.groups||[]).forEach(function(g){{
      h+='<h3>'+_esc2(g.label)+' <span class=hint>('+g.items.length+')</span></h3>';
      h+='<div class="cap-list">';
      g.items.forEach(function(it){{
        var uso = it.uso==null ? 'N/U' : (it.uso+' usos');
        var fecha = (it.last_used_label==='nunca'||!it.last_used_label) ? 'N/U' : _diasDesde(it.last_used_label);
        var cuando = it.cuando ? '<div class="cap-cuando"><b>Cuándo:</b> '+_esc2(it.cuando)+'</div>' : '';
        h+='<div class="cap-row cap-'+it.verdict+'" id="caprow-'+listId+'-'+_slug(it.name)+'">'
          +'<span class="cap-verdict">'+_esc2(it.verdict_label)+'</span>'
          +'<span class="cap-name">'+_esc2(it.name)+(it.redundante?' <span class="cap-flag">duplicada</span>':'')+'</span>'
          +'<span class="cap-uso">'+_esc2(uso)+'<br><span class=hint>'+_esc2(fecha)+'</span></span>'
          +'<span class="cap-desc">'+_esc2(it.desc)+cuando+'</span>'
          +'<span class="cap-test" id="captest-'+listId+'-'+_slug(it.name)+'"></span>'
          +'</div>';
      }});
      h+='</div>';
    }});
    document.getElementById(listId).innerHTML=h||'<p class=hint>Sin elementos.</p>';
  }}).catch(function(){{ _markOffline(sec,false); }});
}}
function _slug(s){{ return (s||'').replace(/[^a-zA-Z0-9]/g,'_'); }}
function _diasDesde(iso){{
  if(!iso) return 'nunca';
  var d=Math.floor((Date.now()-new Date(iso+'T00:00:00').getTime())/86400000);
  return d<=0?'hoy':(d===1?'ayer':('hace '+d+' días'));
}}
function testCap(cat, listId){{
  var out=document.getElementById(listId+'-test');
  if(out) out.textContent='probando…';
  fetch('/cap/test/'+cat).then(function(r){{return r.json();}}).then(function(d){{
    if(d.available===false){{ if(out) out.textContent='no disponible'; return; }}
    var s=d.summary;
    if(out) out.innerHTML=' → <b>'+s.ok+'/'+s.total+'</b> OK'+(s.fail?(' · <span style="color:#cf222e">'+s.fail+' fallan</span>'):'');
    (d.results||[]).forEach(function(r){{
      var cell=document.getElementById('captest-'+listId+'-'+_slug(r.name));
      if(cell){{ cell.textContent=r.ok?'✅':'❌'; cell.title=r.detail; }}
    }});
  }}).catch(function(){{ if(out) out.textContent='error al probar'; }});
}}
function loadCapSkills(){{ loadCap('skills','cap-skills','capskills'); }}
function loadCapAgents(){{ loadCap('agents','cap-agents','capagents'); }}
function loadCapMcp(){{ loadCap('mcp','cap-mcp','capmcp'); }}
function loadCapApi(){{ loadCap('api','cap-api','capapi'); }}
function loadAtomFunc(){{
  function render(d){{
    _markOffline('atomos-func',true);
    var atoms=(d&&d.atoms)||[], groups={{}};
    atoms.forEach(function(a){{var k=a.artifact_type||'(sin clasificar)';(groups[k]=groups[k]||[]).push(a);}});
    var keys=Object.keys(groups).sort(function(x,y){{return groups[y].length-groups[x].length;}});
    var h='<div class=hint style="margin-bottom:.6rem">'+atoms.length+' átomos · '+keys.length+' familias de patrón</div>';
    keys.forEach(function(k){{
      var list=groups[k];
      h+='<details class=afgroup><summary><b>'+_esc2(k)+'</b> <span class=hint>('+list.length+')</span></summary><div class=afbody>';
      list.forEach(function(a){{h+='<div class=afrow><span class=atom-nm>'+_esc2(a.name)+'</span> <span class=atom-proj>'+_esc2(a.project||'')+'</span> <span class=hint>'+_esc2(a.problem_class||'')+'</span></div>';}});
      h+='</div></details>';
    }});
    document.getElementById('atomfunc-list').innerHTML=h||'<p class=hint>Sin átomos.</p>';
  }}
  if(window._atomsData) {{ render(window._atomsData); return; }}
  fetch('/atoms').then(function(r){{return r.json();}}).then(render).catch(function(){{_markOffline('atomos-func',false);}});
}}
function _qRow(m){{
  var pct=(m.clean_rate==null)?0:m.clean_rate;
  var col=m.never_clean?'var(--danger)':(m.last_status==='issues'?'var(--warn)':'var(--ok)');
  var badge=m.never_clean?'<span class="qbadge q-never">nunca pasa</span>'
    :(m.last_status==='issues'?'<span class="qbadge q-dirty">falla ahora</span>'
    :'<span class="qbadge q-ok">limpio</span>');
  var ext=(m.kind!=='commit'&&!m.in_repo)?'<span class="qbadge" style="background:#6e7781;color:#fff" title="No existe en el repo del motor; es de otro proyecto/cliente">externo</span>':'';
  var when=m.last_ts?(' · último gate '+String(m.last_ts).slice(0,10)):'';
  return '<div class=qrow><div class=qname>'+_esc2(m.module_name)+badge+ext+'</div>'+
    '<div class=qbar><div class=qbarfill style="width:'+pct+'%;background:'+col+'"></div></div>'+
    '<div class=qmeta>'+pct+'% limpio · '+m.clean+'/'+m.total+' runs · '+m.issues+' issues'+when+'</div></div>';
}}
/* ---- 💸 Routing — observatorio de gobierno de modelos ---- */
function loadRouting(){{
  fetch('/routing').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('routing',true);
    if(!d||!d.available){{
      document.getElementById('rt-cards').innerHTML='<p class=hint>'+_esc2((d&&d.reason)||'datos de routing no disponibles')+'</p>';
      return;
    }}
    /* --- tarjeta de alerta Fable (P2-D) --- */
    var fableCt=(d.by_model&&d.by_model.fable)||0;
    var alertEl=document.getElementById('rt-alert');
    if(alertEl){{
      if(fableCt>5){{
        alertEl.innerHTML='<div class="st-item down" style="margin-bottom:1rem">⚠️ Fable dispatches: '+fableCt
          +' — revisar; target ≈0 en subagentes (regla H2 / Fable-Gate). Fable=5x más caro que Sonnet.</div>';
      }} else {{
        alertEl.innerHTML='<div class="st-item ok" style="margin-bottom:1rem">✅ Fable dispatches: '+fableCt
          +' — dentro del límite (≤5 en ventana). Regla H2 cumplida.</div>';
      }}
    }}
    /* --- tarjetas resumen --- */
    var disc=d.discipline_pct==null?'—':d.discipline_pct+'%';
    var discColor=(d.discipline_pct!=null&&d.discipline_pct>=80)?'#1a7f37':'#cf222e';
    var discCard='<div class=lc style="border-left:4px solid '+discColor+'"><b style="color:'+discColor+';font-size:1.4rem">'+_esc2(disc)+'</b>'
      +'<span>disciplina model= <span class=hint>(target ≥80%)</span></span></div>';
    var cards=discCard
      +_card(d.dispatches,'Agent() despachados',1)
      +_card(d.explicit_model,'con model= explícito',1)
      +_card(d.inherited,'heredaron hilo',1)
      +_card(_esc2(d.session_model||'—'),'modelo del hilo',1)
      +_card(d.cost_units_relative,'unidades costo rel.',1);
    document.getElementById('rt-cards').innerHTML=cards;
    /* --- desglose por modelo --- */
    var bm=d.by_model||{{}};
    var bmKeys=Object.keys(bm).sort();
    var bmHtml='<h3>Por modelo</h3><div class="live-cards">';
    bmKeys.forEach(function(k){{ bmHtml+=_card(bm[k],_esc2(k)); }});
    bmHtml+='</div>';
    if(d.cost_note) bmHtml+='<p class=hint>'+_esc2(d.cost_note)+'</p>';
    document.getElementById('rt-bymodel').innerHTML=bmHtml;
    /* --- desglose por subagent type --- */
    var bs=d.by_subagent||{{}};
    var bsKeys=Object.keys(bs);
    var bsHtml='<h3>Top subagent types</h3><table class=mem-tbl><thead><tr><th>Tipo</th><th class=num>Llamadas</th></tr></thead><tbody>';
    bsKeys.forEach(function(k){{ bsHtml+='<tr><td>'+_esc2(k)+'</td><td class=num>'+bs[k]+'</td></tr>'; }});
    bsHtml+='</tbody></table>';
    document.getElementById('rt-bysubagent').innerHTML=bsHtml;
    /* --- desglose por intent --- */
    var bi=d.by_intent||{{}};
    var biKeys=Object.keys(bi).sort(function(a,b){{return bi[b]-bi[a];}});
    var biHtml='<h3>Por intención</h3><div class="live-cards">';
    biKeys.forEach(function(k){{ biHtml+=_card(bi[k],_esc2(k)); }});
    biHtml+='</div>';
    document.getElementById('rt-byintent').innerHTML=biHtml;
  }}).catch(function(){{_markOffline('routing',false);}});
}}
function loadQuality(){{
  fetch('/quality').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('calidad',true);
    if(!d||!d.available){{document.getElementById('qual-list').innerHTML='<p class=hint>'+_esc2((d&&d.reason)||'sin datos de calidad')+'</p>';return;}}
    var t=d.totals||{{}};
    document.getElementById('qual-summary').innerHTML=
      _card(t.total,'gates corridos')+
      _card(t.modules_dirty_motor,'del MOTOR sucios 🟠')+
      _card(t.modules_dirty_otros,'de otros proyectos (clientes/labs)')+
      _card(t.modules_never_clean,'nunca pasan el gate 🔴')+
      _card(d.last_gate?String(d.last_gate).slice(0,10):'—','último gate');
    var mods=d.top_issues||[];
    var motor=mods.filter(function(m){{return m.kind!=='commit'&&m.in_repo;}});
    var ext=mods.filter(function(m){{return m.kind!=='commit'&&!m.in_repo;}});
    var commit=mods.filter(function(m){{return m.kind==='commit';}});
    var h='<h3>Módulos del motor — peor estado primero (arregla los 🔴 primero)</h3>'+
      (motor.map(_qRow).join('')||'<p class=hint>sin módulos del motor con deuda 🎉</p>');
    if(ext.length) h+='<h3 style="margin-top:1.2rem">Otros proyectos <span class=hint style="font-weight:400">— módulos de clientes/labs, NO del motor (el gate escribe en una DB compartida)</span></h3>'+ext.map(_qRow).join('');
    if(commit.length) h+='<h3 style="margin-top:1.2rem">Gates de commit <span class=hint style="font-weight:400">— no son módulos de código</span></h3>'+commit.map(_qRow).join('');
    document.getElementById('qual-list').innerHTML=h;
  }}).catch(function(){{_markOffline('calidad',false);}});
}}
function loadBriefs(){{
  fetch('/briefs?limit=15').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('briefs',true);
    var bs=(d&&d.briefs)||[];
    var h='<p class=hint>'+((d&&d.total_in_db)||bs.length)+' sesiones en memoria · mostrando '+bs.length+' recientes</p>';
    bs.forEach(function(b){{
      h+='<div class=brief-card><div class=brief-h><span class=atom-proj>'+_esc2(b.project||'(sin proyecto)')+'</span>'+
         '<span class=st-date>'+_esc2(String(b.created_at||'').slice(0,10))+'</span></div>';
      if(b.request_short) h+='<div class=brief-l><b>pidió:</b> '+_esc2(b.request_short)+'</div>';
      if(b.learned_short) h+='<div class=brief-l><b>aprendió:</b> '+_esc2(b.learned_short)+'</div>';
      if(b.completed_short) h+='<div class=brief-l><b>completó:</b> '+_esc2(b.completed_short)+'</div>';
      h+='</div>';
    }});
    document.getElementById('briefs-list').innerHTML=h||'<p class=hint>sin briefs</p>';
  }}).catch(function(){{_markOffline('briefs',false);}});
}}
var _statusData=null;
function _stMetricHtml(it){{
  var cls=(it.kind==='state')?' st-'+it.status:'';
  return '<div class="st-metric'+cls+'">'+_esc2(it.metric)+'</div>';
}}
function _stDate(it){{ return '<span class=st-date>'+(it.updated?('🕑 '+_esc2(it.updated)):'en vivo')+'</span>'; }}
var _stFilter='all';
function loadStatus(){{
  fetch('/status').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('estado',true); _statusData=d; _stFilter='all';
    var ov=document.getElementById('st-overall'), box=document.getElementById('st-items');
    if(!d.available){{ov.innerHTML='';box.innerHTML='<p class=hint>'+_esc2(d.reason||'estado no disponible')+'</p>';return;}}
    var s=d.summary, lab={{ok:'Todo en verde',warn:'Hay cosas que necesitan trabajo',down:'Algo está roto'}};
    // Banner = resumen + filtros INLINE: los conteos filtran el grid de abajo (sin modal redundante).
    ov.innerHTML='<div class="st-banner '+d.overall+'"><span class="st-dot '+d.overall+'"></span>'+
      _esc2(lab[d.overall]||'')+' <span class=hint style="font-weight:400;margin-left:auto">'+
      '<span class="st-fchip active" data-f=all onclick="stFilter(this)">'+(s.ok+s.warn+s.down)+' todos</span> · '+
      '<span class="st-fchip" data-f=ok onclick="stFilter(this)">'+s.ok+' 🟢</span> · '+
      '<span class="st-fchip" data-f=warn onclick="stFilter(this)">'+s.warn+' 🟠</span> · '+
      '<span class="st-fchip" data-f=down onclick="stFilter(this)">'+s.down+' 🔴</span></span></div>';
    _renderStItems();
  }}).catch(function(){{_markOffline('estado',false);}});
}}
function _renderStItems(){{
  if(!_statusData||!_statusData.available) return;
  var box=document.getElementById('st-items'), h='';
  _statusData.items.forEach(function(it){{
    if(_stFilter!=='all'&&it.status!==_stFilter) return;
    h+='<div class="st-item '+it.status+'" data-n="'+_esc2(it.name)+'" onclick="openItemModal(this.dataset.n)" title="ver el detalle de '+_esc2(it.name)+'">'+
       '<div class=st-name><span class="st-dot '+it.status+'"></span>'+_esc2(it.name)+_stDate(it)+'</div>'+
       _stMetricHtml(it)+'<div class=st-purpose>'+_esc2(it.purpose||'')+'</div></div>';
  }});
  box.innerHTML=h||'<p class=hint>Nada en ese estado.</p>';
}}
function stFilter(el){{
  _stFilter=el.dataset.f;
  el.parentNode.querySelectorAll('.st-fchip').forEach(function(x){{x.classList.remove('active');}});
  el.classList.add('active');
  _renderStItems();
}}
/* 🧬 Átomos de método */
var _atomsData=null;
function loadAtoms(){{
  fetch('/atoms').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('atomos',true); _atomsData=d;
    var box=document.getElementById('atoms-list');
    if(!d.available){{box.innerHTML='<p class=hint>'+_esc2(d.reason||'no disponible')+'</p>';return;}}
    var projs={{}},vals={{}},trs={{}}; d.atoms.forEach(function(a){{projs[a.project]=(projs[a.project]||0)+1;vals[a.value]=(vals[a.value]||0)+1;(a.transfers||[]).forEach(function(t){{trs[t]=(trs[t]||0)+1;}});}});
    // fila 1 — VALOR (incluye Todos)
    var fh='<div class=afilter-row><span class=afilter-lbl>'+d.total+' átomos · Valor</span>'+
      '<button class="hb active" data-f="all" onclick="atomsFilter(this)">Todos ('+d.total+')</button>';
    ['valioso','bajo','descartado','catálogo'].forEach(function(vv){{if(vals[vv])fh+='<button class=hb data-f="'+vv+'" onclick="atomsFilter(this)">'+vv+' ('+vals[vv]+')</button>';}});
    fh+='</div>';
    // fila 2 — PROYECTO (de origen)
    fh+='<div class=afilter-row><span class=afilter-lbl>Proyecto</span>';
    Object.keys(projs).sort(function(a,b){{return projs[b]-projs[a];}}).forEach(function(p){{fh+='<button class=hb data-f="'+_esc2(p)+'" onclick="atomsFilter(this)">'+_esc2(p)+' ('+projs[p]+')</button>';}});
    fh+='</div>';
    // fila 3 — TRANSFIERE A
    if(Object.keys(trs).length){{
      fh+='<div class=afilter-row><span class=afilter-lbl>Transfiere a</span>';
      Object.keys(trs).sort(function(a,b){{return trs[b]-trs[a];}}).forEach(function(t){{fh+='<button class="hb hb-tr" data-f="'+_esc2(t)+'" onclick="atomsFilter(this)" title="átomos que transfieren a este proyecto">→ '+_esc2(t)+' ('+trs[t]+')</button>';}});
      fh+='</div>';
    }}
    document.getElementById('atoms-filters').innerHTML=fh;
    renderAtoms('all');
  }}).catch(function(){{_markOffline('atomos',false);}});
}}
var _VCLS={{valioso:'st-ok','bajo':'st-warn','descartado':'st-down'}};
function renderAtoms(f){{
  var box=document.getElementById('atoms-list'), h='', n=0;
  _atomsData.atoms.forEach(function(a,i){{
    if(f&&f!=='all'&&a.project!==f&&a.value!==f&&(a.transfers||[]).indexOf(f)<0) return;
    n++;
    var tr=(a.transfers&&a.transfers.length)?' · →'+a.transfers.length+' proyectos':'';
    h+='<div class=atom-card data-i="'+i+'" onclick="openAtomModal(this.dataset.i)">'+
       '<div class=atom-h><span class=atom-nm>'+_esc2(a.name)+'</span>'+
       '<span class="atom-val '+(_VCLS[a.value]||'')+'">'+_esc2(a.value)+'</span>'+
       '<span class=atom-proj>'+_esc2(a.project)+'</span></div>'+
       '<div class=atom-meta>'+_esc2(a.problem_class||a.artifact_type||'')+
       (a.artifact_type&&a.problem_class?' · '+_esc2(a.artifact_type):'')+tr+'</div></div>';
  }});
  box.innerHTML=h||'<p class=hint>Sin átomos en ese filtro.</p>';
}}
function atomsFilter(b){{
  document.querySelectorAll('#atoms-filters .hb').forEach(function(x){{x.classList.remove('active');}});
  b.classList.add('active'); renderAtoms(b.dataset.f);
}}
function openAtomModal(i){{
  var a=_atomsData.atoms[i]; if(!a) return;
  var transfers=(a.transfers&&a.transfers.length)?('<p>'+a.transfers.map(function(t){{return '<span class=atom-proj>'+_esc2(t)+'</span>';}}).join(' ')+'</p>'):'<p class=hint>Aún sin transferencias a otros proyectos registradas.</p>';
  var vbc={{valioso:'ok','bajo':'warn','descartado':'down'}};
  document.getElementById('modal-content').innerHTML=
    '<h3>🧬 '+_esc2(a.name)+'</h3>'+
    '<div class="st-banner '+(vbc[a.value]||'ok')+'" style="cursor:default">VALOR: '+_esc2((a.value||'').toUpperCase())+' · '+_esc2(a.problem_class||a.artifact_type||'')+(a.regime?' · '+_esc2(a.regime):'')+'</div>'+
    '<h4>¿Qué es?</h4><p>Un patrón reutilizable de <b>'+_esc2(a.problem_class)+'</b>'+(a.artifact_type?' del tipo <b>'+_esc2(a.artifact_type)+'</b>':'')+', indexado por su ESTRUCTURA para reusarlo entre proyectos. Evidencia: <b>'+_esc2(a.evidence_kind||'?')+'</b>.</p>'+
    '<h4>¿Cómo se compone? (esqueleto)</h4><pre class=rep>'+_esc2(a.skeleton||'—')+'</pre>'+
    '<h4>¿Para qué sirve / dónde aplica y dónde rompe?</h4><p>'+_esc2(a.validity_domain||'—')+'</p>'+
    '<h4>¿En qué proyectos se usa?</h4><p><b>'+_esc2(a.project)+'</b> — adopción: <b>'+_esc2(a.adoption||'?')+'</b>'+(a.scope?' · alcance: '+_esc2(a.scope):' · alcance: global/transferible')+'</p>'+
    '<h4>Transfiere a</h4>'+transfers;
  document.getElementById('modal-box').classList.add('wide');
  document.getElementById('modal').classList.add('show');
}}
/* 💎 Valorización RICE-A+Moat */
var _valorizData=null;
var _VDICT={{adopt:'st-ok',build:'st-warn',omit:'st-down'}};
function loadValorizacion(){{
  fetch('/valorizacion').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('valoriz',true); _valorizData=d;
    var box=document.getElementById('valoriz-summary');
    if(!d.available){{box.innerHTML='<p class=hint>'+_esc2(d.reason||'no disponible')+'</p>';return;}}
    var t=d.totals||{{}};
    box.innerHTML='<div class=live-cards>'+_card(d.total,'átomos valorados')+
      _card(t.adopt||0,'<span class="st-dot ok"></span> adopt',true)+
      _card(t.build||0,'<span class="st-dot warn"></span> build',true)+
      _card(t.omit||0,'<span class="st-dot down"></span> omit',true)+'</div>';
    var fh='<div class=afilter-row><span class=afilter-lbl>Veredicto</span>'+
      '<button class="hb active" data-f="all" onclick="valorizFilter(this)">Todos ('+d.total+')</button>';
    ['adopt','build','omit'].forEach(function(v){{if(t[v])fh+='<button class="hb" data-f="'+v+'" onclick="valorizFilter(this)">'+v+' ('+t[v]+')</button>';}});
    fh+='</div>';
    document.getElementById('valoriz-filters').innerHTML=fh;
    renderValorizacion('all');
  }}).catch(function(){{_markOffline('valoriz',false);}});
}}
function renderValorizacion(f){{
  var box=document.getElementById('valoriz-list');
  if(!_valorizData||!_valorizData.atoms){{box.innerHTML='';return;}}
  var h='',n=0;
  _valorizData.atoms.forEach(function(a,i){{
    if(f&&f!=='all'&&a.verdict!==f) return;
    n++;
    var bar=Math.min(100,Math.round(a.rice_score/13.5*100));
    h+='<div class="atom-card valoriz-card" data-i="'+i+'" onclick="openValorizModal(this.dataset.i)">'+
      '<div class=atom-h>'+
        '<span class=atom-nm>'+_esc2(a.name)+'</span>'+
        '<span class="atom-val '+(_VDICT[a.verdict]||'')+'">'+_esc2(a.verdict)+'</span>'+
        '<span class=atom-proj>'+_esc2(a.project)+'</span>'+
      '</div>'+
      '<div class=atom-meta>'+
        'score <b>'+a.rice_score+'</b> · reach <b>'+a.rice_reach+'</b> · moat <b>'+a.moat+'</b>'+
        (a.problem_class?' · '+_esc2(a.problem_class):'')+
      '</div>'+
      '<div class=rice-bar><div class=rice-fill style="width:'+bar+'%"></div></div>'+
    '</div>';
  }});
  box.innerHTML=h||'<p class=hint>Sin átomos en ese veredicto.</p>';
}}
function valorizFilter(b){{
  document.querySelectorAll('#valoriz-filters .hb').forEach(function(x){{x.classList.remove('active');}});
  b.classList.add('active'); renderValorizacion(b.dataset.f);
}}
function openValorizModal(i){{
  var a=_valorizData.atoms[i]; if(!a) return;
  var tr=(a.transfers&&a.transfers.length)
    ?a.transfers.map(function(t){{return '<span class=atom-proj>'+_esc2(t)+'</span>';}}).join(' ')
    :'<span class=hint>—</span>';
  document.getElementById('modal-content').innerHTML=
    '<h3>💎 '+_esc2(a.name)+'</h3>'+
    '<div class="st-banner '+(_VDICT[a.verdict]||'ok')+'" style="cursor:default">'+
      'VEREDICTO: <b>'+_esc2((a.verdict||'').toUpperCase())+'</b> · '+
      'score <b>'+a.rice_score+'</b> · reach <b>'+a.rice_reach+'</b> · moat <b>'+a.moat+'</b>'+
    '</div>'+
    '<h4>Fórmula RICE-A</h4>'+
    '<dl class=kv>'+
      '<dt>Reach</dt><dd>'+a.rice_reach+' proyectos (source + transfers_to JSON)</dd>'+
      '<dt>Impact</dt><dd>evidencia: <b>'+_esc2(a.evidence_kind||'—')+'</b> (calibrated=3, catalog=2, ninguno=1)</dd>'+
      '<dt>Confidence</dt><dd>adopción: <b>'+_esc2(a.adoption||'—')+'</b> (used=0.9, unused=0.7, null=0.5)</dd>'+
      '<dt>Effort</dt><dd>'+(a.validity_domain?'1 (validity_domain documentado)':'2 (sin validity_domain)')+
      '</dd>'+
      '<dt>Adoption ×</dt><dd>'+_esc2(a.adoption||'—')+' (used=1.0, unused=0.7, null=0.5)</dd>'+
      '<dt>Moat</dt><dd>'+a.moat+' proyectos en transfers_to JSON</dd>'+
    '</dl>'+
    '<h4>¿Dónde aplica y dónde rompe?</h4><p>'+_esc2(a.validity_domain||'— no documentado')+'</p>'+
    '<h4>Transfiere a</h4><p>'+tr+'</p>';
  document.getElementById('modal-box').classList.add('wide');
  document.getElementById('modal').classList.add('show');
}}
/* 🔎 Auditoría del catálogo */
var _auditData=null;
var _ASEV={{warn:'st-warn',info:'st-ok',down:'st-down'}};
var _ATYPE={{
  duplicado:'structural_signature duplicada — posibles átomos redundantes',
  sin_validity:'sin validity_domain — no se sabe dónde aplica/rompe',
  sin_source:'sin source_project — origen desconocido',
  bajo_valor:'marcados [BAJO VALOR]/[DESCARTADO] — candidatos a purgar',
  hueco:'problem_class sin ningún átomo "used" — patrón nunca aplicado'
}};
function loadAuditoria(){{
  fetch('/auditoria').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('auditoria',true); _auditData=d;
    var box=document.getElementById('audit-summary');
    if(!d.available){{box.innerHTML='<p class=hint>'+_esc2(d.reason||'no disponible')+'</p>';return;}}
    var s=d.summary||{{}};
    box.innerHTML='<div class=live-cards>'+
      _card(d.total_atoms,'átomos en store')+
      _card(d.total_findings,'tipos de hallazgo')+
      _card(s.duplicado||0,'duplicados')+
      _card(s.hueco||0,'huecos')+
      _card((s.sin_validity||0)+(s.sin_source||0),'sin metadatos')+
      _card(s.bajo_valor||0,'bajo valor')+'</div>';
    renderAuditoria();
  }}).catch(function(){{_markOffline('auditoria',false);}});
}}
function renderAuditoria(){{
  var box=document.getElementById('audit-findings');
  if(!_auditData||!_auditData.findings){{box.innerHTML='';return;}}
  var h='';
  _auditData.findings.forEach(function(f){{
    var sev=f.severity==='warn'?'warn':(f.severity==='info'?'ok':'down');
    var title=_ATYPE[f.type]||_esc2(f.type);
    h+='<div class="audit-card">'+
      '<div class=audit-h>'+
        '<span class="st-dot '+sev+'"></span>'+
        '<span class=audit-type>'+_esc2(f.type)+'</span>'+
        '<span class=audit-cnt>'+(f.count||'')+'</span>'+
        '<span class=audit-desc>'+title+'</span>'+
      '</div>'+
      '<p class=hint>'+_esc2(f.description)+'</p>';
    if(f.type==='duplicado'){{
      h+='<p class=hint>Signature: <code>'+_esc2(f.signature||'')+'</code> · filas afectadas: '+
        (f.ids||[]).join(', ')+'</p>';
    }} else if(f.type==='hueco'){{
      h+='<ul class="sec-ul">';
      (f.classes||[]).forEach(function(c){{h+='<li><code>'+_esc2(c.problem_class)+'</code> ('+c.total+' en catálogo)</li>';}});
      h+='</ul>';
    }} else if(f.type==='bajo_valor'){{
      h+='<ul class="sec-ul">';
      (f.items||[]).forEach(function(it){{h+='<li><code>'+_esc2(it.label)+'</code> — '+_esc2(it.tag)+'</li>';}});
      h+='</ul>';
    }}
    h+='</div>';
  }});
  box.innerHTML=h||'<p class=hint>Sin hallazgos — el catálogo está limpio.</p>';
}}
/* 📋 Backlog de adopción */
var _backlogData=null;
var _BVDICT={{adopt:'st-ok',build:'st-warn',omit:'st-down'}};
/* colores de chip por estado de fit: candidate=ok, likely-present=info, mismatch=warn, absent=down */
var _FIT_CLS={{candidate:'st-key ok',mismatch:'st-key warn',absent:'st-key down','likely-present':'st-key'}};
var _FIT_LABEL={{candidate:'candidate','likely-present':'ya presente',mismatch:'incompatible',absent:'código ausente'}};
function _fitChips(ft){{
  if(!ft) return '';
  var order=['candidate','likely-present','mismatch','absent'];
  var h='';
  order.forEach(function(k){{
    var n=ft[k]||0; if(!n) return;
    h+=' <span class="'+(_FIT_CLS[k]||'st-key')+'" style="margin-right:.35rem">'+
      n+' '+(_FIT_LABEL[k]||k)+'</span>';
  }});
  return h;
}}
function loadBacklog(){{
  fetch('/backlog').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('backlog',true); _backlogData=d;
    var box=document.getElementById('backlog-summary');
    var chipsBox=document.getElementById('backlog-fit-chips');
    var filtBox=document.getElementById('backlog-filtered');
    if(!d.available){{
      box.innerHTML='<p class=hint>'+_esc2(d.reason||'no disponible')+'</p>';
      chipsBox.innerHTML=''; filtBox.innerHTML='';
      document.getElementById('backlog-list').innerHTML=''; return;
    }}
    /* tarjetas de resumen principales */
    var ft=d.fit_totals||{{}};
    var totalRaw=(ft.absent||0)+(ft.mismatch||0)+(ft['likely-present']||0)+(ft.candidate||0);
    box.innerHTML='<div class=live-cards>'+
      _card(totalRaw,'pares (patrón, destino) evaluados')+
      _card(d.total_items,'candidatos reales')+
      _card(d.total_projects,'proyectos destino con candidatos')+'</div>';
    /* encabezado de verificación con chips de fit */
    chipsBox.innerHTML=
      '<p style="margin:.6rem 0 .25rem;color:var(--text-2);font-size:.93rem">'+
        'De <b style="color:var(--text-1)">'+totalRaw+'</b> pares crudos, '+
        '<b style="color:var(--ok)">'+d.total_items+'</b> candidatos reales &nbsp;&mdash;&nbsp;'+
        _fitChips(ft)+
      '</p>'+
      '<p class=hint style="margin:.2rem 0 .9rem;font-size:.86rem">'+
        'Estos son <b>candidatos a revisar</b>, no builds confirmados: el fit verific&oacute; '+
        'presencia del proyecto + compatibilidad arquitect&oacute;nica, pero la adopci&oacute;n '+
        'real requiere an&aacute;lisis profundo patr&oacute;n a patr&oacute;n.'+
      '</p>';
    /* proyectos descartados (colapsado) */
    var fp=d.filtered_projects||{{}};
    var fpKeys=Object.keys(fp);
    if(fpKeys.length){{
      var _FIT_REASON={{absent:'c&oacute;digo ausente',mismatch:'incompatible (requiere SQL/RLS)','likely-present':'probablemente ya presente'}};
      var li='';
      fpKeys.forEach(function(proj){{
        var info=fp[proj]||{{}};
        var reasonKey=info.reason||'';
        var reasonTxt=_FIT_REASON[reasonKey]||_esc2(reasonKey);
        li+='<li><code>'+_esc2(proj)+'</code> &mdash; '+
          (info.count||0)+' '+( (info.count||0)===1?'&iacute;tem':'&iacute;tems')+
          ' descartados ('+reasonTxt+')</li>';
      }});
      filtBox.innerHTML=
        '<details style="margin:.1rem 0 .9rem">'+
          '<summary style="cursor:pointer;font-size:.9rem;color:var(--action);font-weight:600">'+
            fpKeys.length+' proyecto'+(fpKeys.length===1?'':'s')+' descartado'+(fpKeys.length===1?'':'s')+
            ' (no aparecen en el backlog)'+
          '</summary>'+
          '<ul class="sec-ul" style="margin-top:.4rem">'+li+'</ul>'+
        '</details>';
    }} else {{
      filtBox.innerHTML='';
    }}
    renderBacklog();
  }}).catch(function(){{_markOffline('backlog',false);}});
}}
function renderBacklog(){{
  var box=document.getElementById('backlog-list');
  if(!_backlogData||!_backlogData.by_project){{box.innerHTML='';return;}}
  var h='';
  _backlogData.by_project.forEach(function(pg,gi){{
    var tog=(gi===0)?'&#9660;':'&#9658;';
    var gcls=(gi===0)?'grp open':'grp';
    h+='<div class="'+gcls+'">'+
      '<h3 class=grp-h onclick="this.parentNode.classList.toggle(\\'open\\')">'+
        '<span class=tog>'+tog+'</span> '+
        _esc2(pg.project)+
        (pg.code?' <span class="st-key" style="font-size:.72rem;margin-left:.4rem">'+_esc2(pg.code)+'</span>':'')+
        ' <small>('+pg.count+' candidatos)</small>'+
      '</h3>'+
      '<div class=grp-where>Candidatos verificados: presentes + arquitectura compatible. Ordenados por score RICE-A.</div>'+
      '<div class=grp-body>';
    pg.items.forEach(function(it){{
      var vcls=_BVDICT[it.verdict]||'';
      var bar=Math.min(100,Math.round(it.score/13.5*100));
      h+='<div class="atom-card" style="cursor:default">'+
        '<div class=atom-h>'+
          '<span class=atom-nm>'+_esc2(it.pattern)+'</span>'+
          '<span class="atom-val '+vcls+'">'+_esc2(it.verdict)+'</span>'+
          (it.artifact_type?'<span class="st-key" style="font-size:.7rem">'+_esc2(it.artifact_type)+'</span>':'')+
          '<span class=atom-proj>de: '+_esc2(it.origin)+'</span>'+
        '</div>'+
        '<div class=atom-meta>'+
          'score <b>'+it.score+'</b>'+
          (it.moat?' &middot; moat <b>'+it.moat+'</b>':'')+
          (it.problem_class?' &middot; '+_esc2(it.problem_class):'')+
        '</div>'+
        '<div class=rice-bar><div class=rice-fill style="width:'+bar+'%"></div></div>'+
        (it.why?'<div class="atom-meta" style="margin-top:.3rem;color:var(--text-2)">'+_esc2(it.why)+'</div>':'')+
      '</div>';
    }});
    h+='</div></div>';
  }});
  box.innerHTML=h||'<p class=hint>Backlog vac&iacute;o &mdash; sin candidatos con transferencias a otros proyectos.</p>';
}}
/* 📐 Plantillas (catálogo de skeletons reutilizables) */
var _skelData=null;
function loadSkeletons(){{
  if(_skelData){{_renderSkeletons();return;}}
  fetch('/skeletons').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('plantillas',true); _skelData=d;
    var sumBox=document.getElementById('skel-summary');
    if(!d.available){{
      sumBox.innerHTML='<p class=hint>'+_esc2(d.reason||'plantillas no disponibles')+'</p>';
      document.getElementById('skel-list').innerHTML=''; return;
    }}
    sumBox.innerHTML=
      _card(d.total,'plantillas reutilizables')+
      _card(d.families,'familias de problema');
    _renderSkeletons();
  }}).catch(function(){{_markOffline('plantillas',false);}});
}}
function _renderSkeletons(){{
  var box=document.getElementById('skel-list');
  if(!_skelData||!_skelData.by_family){{box.innerHTML='';return;}}
  var h='';
  _skelData.by_family.forEach(function(fam,gi){{
    var isFirst=(gi===0);
    var tog=isFirst?'&#9660;':'&#9658;';
    var gcls=isFirst?'grp open':'grp';
    h+='<div class="'+gcls+'">'+
      '<h3 class=grp-h onclick="this.parentNode.classList.toggle(\\'open\\')">'+
        '<span class=tog>'+tog+'</span> '+
        _esc2(fam.family)+
        ' <small>('+fam.count+' plantilla'+(fam.count===1?'':'s')+')</small>'+
      '</h3>'+
      '<div class=grp-body>';
    fam.items.forEach(function(it){{
      var originBadge=it.origin
        ?'<span class="kind read" style="margin-left:.4rem">'+_esc2(it.origin)+'</span>':'';
      var regimeBadge=it.regime
        ?'<span class="st-key" style="font-size:.7rem;margin-left:.4rem">'+_esc2(it.regime)+'</span>':'';
      h+='<div class=atom-card style="cursor:default">'+
        '<div class=atom-h>'+
          '<span class=atom-nm>'+_esc2(it.name)+'</span>'+
          originBadge+regimeBadge+
          '<span class=atom-proj>'+it.lines+' l&iacute;nea'+(it.lines===1?'':'s')+'</span>'+
        '</div>'+
        (it.problem_class
          ?'<div class=atom-meta>'+_esc2(it.problem_class)+'</div>':'')+
        '<pre class="skel-pre" data-skel="'+gi+'-'+fam.items.indexOf(it)+'"></pre>'+
      '</div>';
    }});
    h+='</div></div>';
  }});
  box.innerHTML=h;
  /* Inyectar el código via textContent — NUNCA innerHTML para evitar XSS */
  _skelData.by_family.forEach(function(fam,gi){{
    fam.items.forEach(function(it,ii){{
      var pre=box.querySelector('[data-skel="'+gi+'-'+ii+'"]');
      if(pre) pre.textContent=it.skeleton;
    }});
  }});
}}
function openItemModal(name){{
  var it=null; if(_statusData&&_statusData.items) _statusData.items.forEach(function(x){{if(x.name===name)it=x;}});
  if(!it) return;
  var fecha=it.updated?(' <span class=hint style="font-weight:400;margin-left:auto">última actualización: '+_esc2(it.updated)+'</span>'):'';
  document.getElementById('modal-content').innerHTML=
    '<h3><span class="st-dot '+it.status+'"></span> '+_esc2(name)+'</h3>'+
    '<p class=hint>'+_esc2(it.purpose||'')+'</p>'+
    '<div class="st-banner '+it.status+'" style="cursor:default">'+_esc2(it.metric)+fecha+'</div>'+
    '<div id=item-detail><p class=hint>cargando…</p></div>';
  document.getElementById('modal-box').classList.add('wide');
  document.getElementById('modal').classList.add('show');
  loadItemDetail(name);
}}
function _memDetailHtml(d){{
  if(!d.available) return '<p class=hint>'+_esc2(d.reason||'no disponible')+'</p>';
  var t=d.totals, r=d.recall||{{}};
  var rate=(r.useful_rate==null?'—':Math.round(r.useful_rate*100)+'%');
  var h='<div class=live-cards>'+_card(t.decisions,'decisiones')+_card(t.guards,'guards')+
    _card(t.digests,'digests')+_card(t.vectors,'vectores')+
    _card(r.feedback_total,'recall feedback')+_card(rate,'recall útil')+'</div>';
  h+='<p class=hint>ℹ️ «Decisiones» = la bitácora de lo decidido por proyecto. El conocimiento reutilizable (los <i>facts</i>) se cuenta aparte en la pestaña 🧬 Átomos, y la procedencia interna no se lista aquí; por eso este número es menor que el total de filas de la tabla <code>decisions</code>.</p>';
  if(r.useful_rate!=null && r.useful_rate<0.6) h+='<p class=hint>🟠 recall útil '+rate+' (&lt;60%) — puede haber ruido en lo recuperado; revisa si las decisiones recuperadas ayudan.</p>';
  var poor=d.by_client.filter(function(c){{return c.decisions<5||c.guards===0;}}).length;
  h+='<h4>Salud de captura por proyecto <small class=hint>(clic en un proyecto para ver qué es)</small></h4>';
  if(poor) h+='<p class=hint>🔴 '+poor+' proyecto(s) con captura pobre — ingiere decisiones/guards (aris_ingest) para que ARIS4U cebe a Claude mejor ahí.</p>';
  h+='<table class=mem-tbl><thead><tr><th>Proyecto</th><th>Captura</th><th class=num>Dec</th><th class=num>Guards</th><th class=num>Digests</th></tr></thead><tbody>';
  d.by_client.forEach(function(c){{
    var fl=(c.decisions===0)?'<span class="qbadge q-never">vacío</span>'
      :(c.decisions<5)?'<span class="qbadge q-dirty">pobre</span>'
      :(c.guards===0)?'<span class="qbadge q-dirty">sin guards</span>'
      :'<span class="qbadge q-ok">ok</span>';
    h+='<tr class=proj-row data-label="'+_esc2(c.label)+'" data-about="'+_esc2(c.about||'')+'" onclick="showProjAbout(this)">'+
       '<td>'+_esc2(c.label)+'</td><td>'+fl+'</td><td class=num>'+c.decisions+'</td><td class=num>'+c.guards+'</td><td class=num>'+c.digests+'</td></tr>';
  }});
  h+='</tbody></table>';
  if(d.by_source && d.by_source.length){{
    h+='<h4>🌐 Origen de átomos globales <small class=hint>(conocimiento transferible — NO scopeado a un proyecto, pero sabemos de dónde salió)</small></h4>';
    h+='<table class=mem-tbl><thead><tr><th>Proyecto de origen</th><th class=num>Átomos</th></tr></thead><tbody>';
    d.by_source.forEach(function(s){{
      h+='<tr class=proj-row data-label="'+_esc2(s.label)+'" data-about="'+_esc2(s.about||'')+'" onclick="showProjAbout(this)">'+
         '<td>'+_esc2(s.label)+'</td><td class=num>'+s.decisions+'</td></tr>';
    }});
    h+='</tbody></table>';
  }}
  return h+'<div id=proj-about class=proj-about></div>';
}}
function showProjAbout(tr){{ var el=document.getElementById('proj-about');
  if(el) el.innerHTML='<b>'+_esc2(tr.dataset.label)+'</b> — '+_esc2(tr.dataset.about); }}
function _hooksDetailHtml(d){{
  var h='<table class=hk-tbl><thead><tr><th>Evento</th><th>Repo</th><th>Global</th></tr></thead><tbody>';
  (d.events||[]).forEach(function(e){{h+='<tr><td>'+(e.wired?'<span class=hk-yes>●</span> ':'<span class=hk-no>○</span> ')+_esc2(e.event)+'</td><td>'+_cmds(e.repo)+'</td><td>'+_cmds(e['global'])+'</td></tr>';}});
  return h+'</tbody></table>';
}}
function _ampDetailHtml(d){{
  if(!d.available) return '<p class=hint>no disponible</p>';
  var pct=Math.min(100,Math.round(d.labeled/d.threshold*100));
  return '<div class=prog-wrap><b>'+d.labeled+' / '+d.threshold+'</b> etiquetas<div class=prog-bar><div class=prog-fill style="width:'+pct+'%"></div></div></div>'+
    '<p class=hint>Llamadas: '+d.calls+' · disponibilidad '+Math.round(d.availability_rate*100)+'% · útiles '+d.useful+'/'+d.labeled+'</p>'+
    '<p class=hint>Para subir el progreso: usa el cuerpo local en tu trabajo y etiqueta en la pestaña 🔬 Amplificador.</p>';
}}
function loadItemDetail(name){{
  var box=document.getElementById('item-detail'); function set(h){{if(box)box.innerHTML=h;}}
  function j(u,fn){{fetch(u).then(function(r){{return r.json();}}).then(fn).catch(function(){{set('<p class=hint>no disponible (¿servidor?).</p>');}});}}
  if(name==='Memoria') j('/memory',function(d){{set(_memDetailHtml(d));}});
  else if(name==='Recall útil') j('/memory',function(d){{set(d.available?_recallBox(d.recall)+'<p class=hint>El recall trae memoria relevante a cada mensaje; esto mide si de verdad ayudó (se etiqueta con el uso).</p>':'<p class=hint>no disponible</p>');}});
  else if(name==='Hooks (reflejos)') j('/hooks',function(d){{set(_hooksDetailHtml(d));}});
  else if(name==='Amplificador') j('/amplifier',function(d){{set(_ampDetailHtml(d));}});
  else if(name==='Herramientas MCP') set('<p>Las 7 herramientas que ARIS4U expone a Claude:</p><ul class=sec-ul><li><b>aris_search</b> — buscar en la memoria</li><li><b>aris_recall_client</b> — traer decisiones de un proyecto</li><li><b>aris_ingest</b> — guardar una decisión o guard</li><li><b>aris_dialectic</b> — revisar código (Builder/Reviewer/Security)</li><li><b>aris_structure</b> — estructurar una idea (cuerpo local)</li><li><b>aris_critique</b> — criticar una respuesta (cuerpo local)</li><li><b>aris_health</b> — salud del sistema</li></ul><p class=hint>Para usarlas, ve a la pestaña 🔭 MCP.</p>');
  else if(name==='Cuerpo local (MLX)') set('<p>Un modelo que corre en TU Mac y estructura/critica para potenciar a Claude, sin enviar nada afuera.</p><p class=hint>Encenderlo: <code>bash ~/projects/aris4u/tools/mlx_serve.sh start</code> (necesita ~24GB libres).</p>');
  else if(name==='Ollama (local)') set('<p>Corre modelos locales (embeddings para la búsqueda semántica) sin enviar datos a la nube.</p><p class=hint>Si está caído: abre la app de Ollama o <code>ollama serve</code>.</p>');
  else if(name==='Búsqueda semántica') set('<p>Encuentra memoria por <b>significado</b>, no solo por palabra exacta — usando vectores.</p><p class=hint>Se nutre de Ollama (embeddings) + el sidecar de vectores.</p>');
  else set('<p class=hint>Sin detalle adicional.</p>');
}}
document.querySelectorAll('.nav-b').forEach(function(b){{
  b.addEventListener('click',function(){{ onSectionShow(b.dataset.s); }});
}});
/* A1 — Memoria */
function loadMemory(){{
  fetch('/memory').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('memoria',true);
    if(!d.available){{document.getElementById('mem-cards').innerHTML='<p class=hint>'+_esc2(d.reason||'memoria no disponible')+'</p>';return;}}
    var t=d.totals;
    document.getElementById('mem-cards').innerHTML=
      _card(t.decisions,'decisiones')+_card(t.guards,'guards')+_card(t.digests,'digests')+_card(t.vectors,'vectores')
      +_card(t.observations_local!=null?t.observations_local:'—','Observaciones')
      +_card(t.cowork_comments!=null?t.cowork_comments:'—','Cowork · comentarios');
    var poor=d.by_client.filter(function(c){{return c.decisions<5||c.guards===0;}}).length;
    var rr=d.recall||{{}};
    var ch='<p class=hint>ℹ️ «Decisiones» = la bitácora de lo decidido por proyecto. El conocimiento reutilizable (los <i>facts</i>) se cuenta aparte en la pestaña 🧬 Átomos, y la procedencia interna no se lista aquí; por eso este número es menor que el total de filas de la tabla <code>decisions</code>.</p>';
    ch+='<h3>Salud de captura por proyecto <small class=hint>(clic en un proyecto para filtrar)</small></h3>';
    if(poor) ch+='<p class=hint>🔴 '+poor+' proyecto(s) con captura pobre — ingiere decisiones/guards (aris_ingest) para que ARIS4U cebe a Claude mejor ahí.</p>';
    if(rr.useful_rate!=null && rr.useful_rate<0.6) ch+='<p class=hint>🟠 recall útil '+Math.round(rr.useful_rate*100)+'% (&lt;60%) — posible ruido en lo recuperado.</p>';
    ch+='<table class=mem-tbl><thead><tr><th>Proyecto</th><th>Captura</th><th class=num>Decisiones</th><th class=num>Guards</th><th class=num>Digests</th></tr></thead><tbody>';
    d.by_client.forEach(function(c){{
      var fl=(c.decisions===0)?'<span class="qbadge q-never">vacío</span>'
        :(c.decisions<5)?'<span class="qbadge q-dirty">pobre</span>'
        :(c.guards===0)?'<span class="qbadge q-dirty">sin guards</span>'
        :'<span class="qbadge q-ok">ok</span>';
      ch+='<tr><td><a href="#" class=mem-clink data-c="'+_esc2(c.client)+'" title="'+_esc2(c.about||'')+'">'+_esc2(c.label)+'</a></td><td>'+fl+'</td><td class=num>'+c.decisions+'</td><td class=num>'+c.guards+'</td><td class=num>'+c.digests+'</td></tr>';
    }});
    var bs='';
    if(d.by_source && d.by_source.length){{
      bs='<h3>🌐 Origen de átomos globales <small class=hint>(conocimiento transferible — NO es bitácora de decisiones; son patrones reutilizables)</small></h3>'+
         '<table class=mem-tbl><thead><tr><th>Proyecto de origen</th><th class=num>Átomos</th></tr></thead><tbody>';
      d.by_source.forEach(function(s){{bs+='<tr title="'+_esc2(s.about||'')+'"><td>'+_esc2(s.label)+'</td><td class=num>'+s.decisions+'</td></tr>';}});
      bs+='</tbody></table>';
    }}
    document.getElementById('mem-clients').innerHTML=ch+'</tbody></table>'+_recallBox(d.recall)+bs;
    document.querySelectorAll('.mem-clink').forEach(function(a){{a.addEventListener('click',function(e){{e.preventDefault();var s=document.getElementById('mem-client');if(s){{s.value=a.dataset.c;memSearch();document.getElementById('mem-q').scrollIntoView({{behavior:'smooth',block:'center'}});}}}});}});
    loadFacets(); memSearch();
  }}).catch(function(){{_markOffline('memoria',false);}});
}}
var _memFacetsLoaded=false;
function loadFacets(){{
  if(_memFacetsLoaded) return;
  fetch('/memory/facets').then(function(r){{return r.json();}}).then(function(d){{
    if(!d.available) return;
    var cs=document.getElementById('mem-client'),ds=document.getElementById('mem-domain'),ts=document.getElementById('mem-type');
    if(cs) d.clients.forEach(function(c){{var o=document.createElement('option');o.value=c;o.textContent=c;cs.appendChild(o);}});
    if(ds) d.domains.forEach(function(x){{var o=document.createElement('option');o.value=x;o.textContent=x;ds.appendChild(o);}});
    if(ts) (d.types||[]).forEach(function(x){{var o=document.createElement('option');o.value=x;o.textContent=x;ts.appendChild(o);}});
    _memFacetsLoaded=true;
  }}).catch(function(){{}});
}}
function memToggle(b){{ b.classList.toggle('on'); memSearch(); }}
var _memTimer=null;
function memSearchDebounced(){{ if(_memTimer)clearTimeout(_memTimer); _memTimer=setTimeout(memSearch,250); }}
function _val(id){{ var el=document.getElementById(id); return el?el.value:''; }}
function _on(id){{ var el=document.getElementById(id); return el&&el.classList.contains('on'); }}
function memSearch(){{
  var p='?limit=150&q='+encodeURIComponent(_val('mem-q'))+'&client='+encodeURIComponent(_val('mem-client'))+
    '&domain='+encodeURIComponent(_val('mem-domain'))+'&mem_type='+encodeURIComponent(_val('mem-type'))+
    (_on('mf-locked')?'&locked=1':'')+(_on('mf-stale')?'&stale=30':'');
  var res=document.getElementById('mem-results'),cnt=document.getElementById('mem-count');
  if(cnt) cnt.textContent='buscando…';
  fetch('/memory/search'+p).then(function(r){{return r.json();}}).then(function(d){{
    if(!d.available){{if(cnt)cnt.textContent='';res.innerHTML='<p class=hint>'+_esc2(d.reason||'no disponible')+'</p>';return;}}
    if(cnt) cnt.textContent=d.count+' resultado'+(d.count===1?'':'s');
    var h='';
    d.decisions.forEach(function(x){{
      h+='<div class="mem-result'+(x.locked?' locked':'')+'"><div class=mr-meta>'+(x.locked?'<span class=lock>🔒</span>':'')+
         '<span class=tag>'+_esc2(x.client)+'</span><span class=tag>'+_esc2(x.domain||'general')+'</span>'+
         '<span class=mr-when>'+_esc2((x.created_at||'').substr(0,10))+'</span></div>'+_esc2(x.decision)+'</div>';
    }});
    d.guards.forEach(function(x){{
      h+='<div class="mem-result guard"><div class=mr-meta><span class=tag>guard</span><span class=tag>'+_esc2(x.severity||'med')+
         '</span><span class=tag>'+_esc2(x.client)+'</span><span class=mr-when>'+_esc2((x.created_at||'').substr(0,10))+
         '</span></div>'+_esc2(x.pattern)+' → '+_esc2(x.prevention)+'</div>';
    }});
    res.innerHTML=h||'<p class=hint>Sin resultados para esos filtros.</p>';
  }}).catch(function(){{if(cnt)cnt.textContent='';res.innerHTML='<p class=hint>Búsqueda no disponible (¿servidor?).</p>';}});
}}
function _recallBox(r){{
  if(!r) return '';
  var rate=(r.useful_rate==null?'—':(r.useful_rate*100).toFixed(0)+'%');
  return '<h3>Medidor de recall</h3><div class=live-cards>'+_card(r.feedback_total,'feedback')+_card(rate,'útil')+_card(r.recalls_in_window,'recalls (ventana)')+'</div>'+(r.last_recall_query?'<p class=hint>Último recall: <code>'+_esc2(r.last_recall_query)+'</code></p>':'');
}}
/* A2 — Pulso (telemetría) */
var _pulseES=null;
var _EVH={{auto_recall:['🧠 Recall de memoria','buscó contexto útil para tu mensaje'],
model_hint:['🎯 Sugerencia de modelo','eligió el modelo según la dificultad'],
model_route:['🔀 Ruteo de modelo','envió el trabajo al modelo elegido'],
depth_inject:['📏 Profundidad','ajustó cuánto razonar'],
mcp_tool:['🔭 Herramienta MCP','se usó una herramienta de ARIS4U'],
subagent_start:['🤖 Sub-agente','delegó a un agente en paralelo'],
agent_dispatched:['🤖 Sub-agente','despachó una subtarea'],
agent_verify_no_changes:['✅ Agente sin cambios','el agente terminó sin tocar archivos'],
agent_output_verified:['✅ Verificación','revisó el resultado de un agente'],
session_briefing:['📋 Briefing','cargó el contexto al empezar'],
capture_commit:['💾 Commit','registró un commit en la memoria'],
secret_redacted:['🔒 Secreto ocultado','evitó filtrar una credencial']}};
function _evMeta(e){{ if(e.label) return [e.label,e.desc||'']; var t=e.type||e.event||e.hook||'?';
  if(_EVH[t]) return _EVH[t]; return ['⚙ '+String(t).replace(/_/g,' '),'actividad interna']; }}
function _evRow(e,isNew){{var m=_evMeta(e);return '<div class="ev'+(isNew?' new':'')+'"><span class=t>'+
  _esc2((e.ts||'').substr(11,8))+'</span><span class=ty>'+_esc2(m[0])+'</span><span class=s>'+_esc2(m[1])+'</span></div>';}}
function loadTelemetry(){{
  fetch('/telemetry?limit=80').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('pulso',true);
    if(!d.available){{document.getElementById('tel-feed').innerHTML='<p class=hint>'+_esc2(d.reason||'sin telemetría')+'</p>';return;}}
    var agg='';Object.keys(d.by_type).slice(0,8).forEach(function(k){{agg+=_card(d.by_type[k],k);}});
    document.getElementById('tel-agg').innerHTML=agg;
    var fh='';d.recent.forEach(function(e){{fh+=_evRow(e,false);}});
    document.getElementById('tel-feed').innerHTML=fh;
  }}).catch(function(){{_markOffline('pulso',false);}});
}}
function startPulse(btn){{
  if(_pulseES) return;
  loadTelemetry();
  if(typeof EventSource==='undefined'){{document.getElementById('pulse-state').textContent='(sin EventSource)';return;}}
  _pulseES=new EventSource('/telemetry/stream');
  document.getElementById('pulse-state').textContent='● en vivo';
  var feed=document.getElementById('tel-feed');
  _pulseES.onmessage=function(ev){{try{{var e=JSON.parse(ev.data);feed.insertAdjacentHTML('afterbegin',_evRow(e,true));while(feed.children.length>300)feed.removeChild(feed.lastChild);}}catch(_e){{}}}};
  _pulseES.onerror=function(){{document.getElementById('pulse-state').textContent='(stream cerrado)';stopPulse();}};
}}
function stopPulse(){{if(_pulseES){{_pulseES.close();_pulseES=null;var st=document.getElementById('pulse-state');if(st)st.textContent='⏸ pausado';}}}}
/* A4 — Hooks */
function _cmds(arr){{if(!arr||!arr.length)return '<span class=hk-no>—</span>';return arr.map(function(c){{return '<code>'+_esc2(c)+'</code>';}}).join(' ');}}
function loadPhiGuardBlocks(){{
  fetch('/phi-guard-blocks').then(function(r){{return r.json();}}).then(function(d){{
    var el=document.getElementById('phi-guard-metric');
    if(!el)return;
    if(!d.available){{el.innerHTML='<p class=hint>phi_guard: '+_esc2(d.reason||'no disponible')+'</p>';return;}}
    var last=d.last_ts?(' — último: '+_esc2(d.last_ts.slice(0,16).replace('T',' '))+' ('+_esc2(d.last_tool||'?')+')'):' — sin registro reciente';
    el.innerHTML='<div class="live-card live-card-warn"><div class="lc-num">'+d.total+'</div>'
      +'<div class="lc-label">bloqueos phi_guard</div>'
      +'<div class="lc-hint">acciones PHI bloqueadas hacia destinos externos'+last+'</div></div>';
  }}).catch(function(){{
    var el=document.getElementById('phi-guard-metric');
    if(el)el.innerHTML='<p class=hint>phi_guard: servidor no disponible</p>';
  }});
}}
function loadHooks(){{
  fetch('/hooks').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('hooks',true);
    var wl=d.window_lines||1500;var we=d.window||'?';
    var hmap=d.handler_map||{{}};
    var hints=d.uncabled_hints||{{}};
    var wiredN=d.wired_count!=null?d.wired_count:'?';
    var totalN=d.total_lifecycle!=null?d.total_lifecycle:9;
    var h='<h3>Eventos cableados <span class=hint>('+wiredN+' de '+totalN+')</span></h3>'
      +'<p class=hint>Disparos = ventana de los últimos '+wl+' líneas del log ('+we+' eventos). No es conteo global.</p>'
      +'<table class=hk-tbl><thead><tr><th>Evento</th><th>Repo (plugin)</th><th>Global (~/.claude)</th><th>Disparos</th><th>Último disparo</th></tr></thead><tbody>';
    d.events.forEach(function(e){{
      var cnt=e.count?('<b>'+e.count+'</b>'):'<span class=hk-no>0</span>';
      var lf=e.last_fired?_esc2(e.last_fired.slice(0,16).replace('T',' ')):'<span class=hk-no>—</span>';
      if(e.wired){{
        h+='<tr><td><span class=hk-yes>●</span> '+_esc2(e.event)+'</td><td>'+_cmds(e.repo)+'</td><td>'+_cmds(e['global'])+'</td><td>'+cnt+'</td><td>'+lf+'</td></tr>';
        var evHandlers=hmap[e.event]||[];
        if(evHandlers.length){{
          evHandlers.forEach(function(hh){{
            var hcnt=hh.count?('<b>'+hh.count+'</b>'):'<span class=hk-no>0</span>';
            var hlf=hh.last_fired?_esc2(hh.last_fired.slice(0,16).replace('T',' ')):'<span class=hk-no>—</span>';
            h+='<tr class=hk-sub><td style="padding-left:1.8rem"><span class=hint>↳</span> <code>'+_esc2(hh.handler)+'</code></td><td colspan=2 class=hint>sub-handler</td><td>'+hcnt+'</td><td>'+hlf+'</td></tr>';
          }});
        }}
      }} else {{
        var hintText=hints[e.event]?(' — <span class=hint>'+_esc2(hints[e.event])+'</span>'):'';
        h+='<tr class=hk-uncabled><td><span class=hk-no>○</span> '+_esc2(e.event)+hintText+'</td><td colspan=2 class=hk-no>sin cablear</td><td><span class=hk-no>—</span></td><td><span class=hk-no>—</span></td></tr>';
      }}
    }});
    document.getElementById('hooks-body').innerHTML=h+'</tbody></table>';
    var f='<h3>Handlers (disparos por fuente)</h3><div class=live-cards>';
    Object.keys(d.fired_by_source).forEach(function(k){{f+=_card(d.fired_by_source[k],k);}});
    document.getElementById('hooks-fired').innerHTML=f+'</div>';
  }}).catch(function(){{_markOffline('hooks',false);}});
}}
/* Cabina del amplificador F1 */
function loadAmplifier(){{
  fetch('/amplifier').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('amp',true);
    if(!d.available){{document.getElementById('amp-head').innerHTML='<p class=hint>'+_esc2(d.reason||'no disponible')+'</p>';return;}}
    renderAmp(d);
  }}).catch(function(){{_markOffline('amp',false);}});
}}
function renderAmp(d){{
  var badge=d.body_up?'<span class="body-badge body-up">● cuerpo encendido</span>':'<span class="body-badge body-down">○ cuerpo frío — arráncalo: mlx_serve.sh start</span>';
  document.getElementById('amp-head').innerHTML='<div class=lc style="min-width:100%"><span>'+badge+'</span></div>'+_card(d.calls,'llamadas')+_card(Math.round(d.availability_rate*100)+'%','disponibilidad')+_card((d.latency_p50/1000).toFixed(0)+'s','latencia p50')+_card(d.useful+'/'+d.labeled,'útiles');
  var pct=Math.min(100,Math.round(d.labeled/d.threshold*100));
  var msg=d.ready_for_calibration?'✅ LISTO para calibrar (§8.5)':('faltan '+(d.threshold-d.labeled)+' para cablear la capa de decisión');
  document.getElementById('amp-progress').innerHTML='<div class=prog-wrap><b>'+d.labeled+' / '+d.threshold+'</b> etiquetas · '+msg+'<div class=prog-bar><div class=prog-fill style="width:'+pct+'%"></div></div></div>';
  var ph='';
  if(!d.pending.length){{ph='<p class=hint>No hay llamadas pendientes. Usa aris_structure/aris_critique en tu trabajo (con el cuerpo encendido) y vuelve a etiquetar.</p>';}}
  else{{d.pending.forEach(function(c){{
    ph+='<div class=amp-row><span class=tool>'+_esc2(c.tool)+'</span><span class=meta>'+_esc2(c.age)+' · '+_esc2(c.backend)+' · '+_esc2(c.chars)+' chars · id:'+_esc2(c.call_id)+'</span><span class=sp></span>'+
      '<button class="lbl-btn lbl-ok" data-cid="'+_esc2(c.call_id)+'" data-u="1">✓ ayudó</button>'+
      '<button class="lbl-btn lbl-no" data-cid="'+_esc2(c.call_id)+'" data-u="0">✗ no</button></div>';
  }});}}
  document.getElementById('amp-pending').innerHTML=ph;
}}
function labelCall(callId,useful){{
  fetch('/amplifier/label',{{method:'POST',body:JSON.stringify({{call_id:callId,useful:useful}})}}).then(function(r){{return r.json();}}).then(function(d){{
    if(d.amplifier) renderAmp(d.amplifier);
  }}).catch(function(){{}});
}}
(function(){{var p=document.getElementById('amp-pending'); if(p) p.addEventListener('click',function(e){{
  var b=e.target.closest('.lbl-btn'); if(!b)return; labelCall(b.dataset.cid, b.dataset.u==='1');
}});}})();
/* A3 — MCP */
function callMcp(tool,keys,btn){{
  var sec=btn.closest('section'); var args={{}};
  (keys||[]).forEach(function(k){{var el=sec.querySelector('.mcp-in[data-k="'+k+'"]');if(el&&el.value.trim())args[k]=el.value.trim();}});
  var out=document.getElementById('mcp-out'); out.classList.add('show'); out.textContent='Ejecutando '+tool+'…';
  var body={{tool:tool,args:args}};
  function send(){{return fetch('/mcp',{{method:'POST',body:JSON.stringify(body)}}).then(function(r){{return r.json();}});}}
  send().then(function(d){{
    if(d.need_confirm){{
      var msg=(d.kind==='write')?'Esto ESCRIBE en la memoria de ARIS4U (locked). ¿Confirmar?':'Esto usa el MODELO LOCAL (puede tardar o estar frío). ¿Ejecutar?';
      if(confirm(msg)){{body.confirm=true;out.textContent='Ejecutando '+tool+'…';send().then(function(d2){{out.textContent=(d2.ok?'✅ ':'⚠️ ')+(d2.output||d2.error||'');}});}}
      else{{out.textContent='(cancelado)';}}
      return;
    }}
    out.textContent=(d.ok?'':'⚠️ ')+(d.output||d.error||'(sin salida)');
  }}).catch(function(){{out.textContent='No se pudo invocar (¿servidor corriendo?).';}});
}}
/* ⚙️ Config */
function loadConfig(){{
  fetch('/config').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('config',true);
    var box=document.getElementById('cfg-body');
    if(!d.available){{box.innerHTML='<p class=hint>'+_esc2(d.reason||'config no disponible')+'</p>';return;}}
    var h='';
    h+='<div class="st-banner ok" style="cursor:default">Modelo por defecto: <b>'+_esc2(d.model_default||'—')+'</b></div>';
    h+='<h3>Entorno / flags</h3>';
    var env=d.env||{{}};
    var envKeys=Object.keys(env);
    if(!envKeys.length){{h+='<p class=hint>Sin flags configurados.</p>';}}
    else{{
      h+='<table class=hk-tbl><thead><tr><th>Flag</th><th>Valor</th></tr></thead><tbody>';
      envKeys.forEach(function(k){{h+='<tr><td><code>'+_esc2(k)+'</code></td><td>'+_esc2(env[k])+'</td></tr>';}});
      h+='</tbody></table>';
    }}
    h+='<h3>MCP cableados</h3>';
    var bySource=d.mcp_by_source||[],dup=d.mcp_duplicated||[];
    if(!bySource.length){{h+='<p class=hint>Sin MCP cableados detectados.</p>';}}
    else{{
      h+='<table class=hk-tbl><thead><tr><th>Nombre</th><th>Origen</th><th>Estado</th></tr></thead><tbody>';
      bySource.forEach(function(m){{
        var isDup=dup.indexOf(m.name)>=0;
        var st=isDup?'<span style="color:var(--warn);font-weight:700">duplicado</span>':'<span class=hk-yes>ok</span>';
        h+='<tr><td><code>'+_esc2(m.name)+'</code></td><td>'+_esc2(m.origin||'global')+'</td><td>'+st+'</td></tr>';
      }});
      h+='</tbody></table>';
    }}
    if(dup.length){{
      h+='<div class="live-offline" style="display:block;background:#2a1a0f;border-color:#6a3f10;color:var(--warn)">'+
         '⚠️ MCP duplicados (activos en repo Y global): '+dup.map(function(n){{return '<code>'+_esc2(n)+'</code>';}}).join(', ')+'</div>';
    }}
    h+='<h3>Ruta del settings</h3><p class=hint><code>'+_esc2(d.settings_path||'—')+'</code></p>';
    box.innerHTML=h;
  }}).catch(function(){{_markOffline('config',false);}});
}}
/* 🗺️ API — manifiesto de endpoints */
var _KIND_CLS={{'read':'read','write':'write','action':'local','stream':'local'}};
function loadApi(){{
  fetch('/manifest').then(function(r){{return r.json();}}).then(function(d){{
    _markOffline('api',true);
    var box=document.getElementById('api-body');
    if(!d.endpoints||!d.endpoints.length){{box.innerHTML='<p class=hint>Manifiesto no disponible.</p>';return;}}
    var h='<div class=live-cards>'+_card(d.count,'endpoints')+'</div>';
    h+='<p>'+_esc2(d.purpose||'')+'</p>';
    h+='<table class=hk-tbl><thead><tr><th>Método</th><th>Ruta</th><th>Tipo</th><th>Propósito</th></tr></thead><tbody>';
    d.endpoints.forEach(function(e){{
      var kc=_KIND_CLS[e.kind]||'';
      h+='<tr'+(e.path==="/mcp"?' class=proj-row onclick="toggleMcpTools(this)"':'')+'>';
      h+='<td><code>'+_esc2(e.method)+'</code></td>';
      h+='<td><code>'+_esc2(e.path)+'</code></td>';
      h+='<td><span class="kind '+kc+'">'+_esc2(e.kind||'')+'</span></td>';
      h+='<td>'+_esc2(e.purpose||'')+'</td>';
      h+='</tr>';
      if(e.path==='/mcp'&&e.tools&&e.tools.length){{
        h+='<tr id="mcp-tools-row" hidden><td colspan=4>';
        h+='<table class=hk-tbl style="margin:.3rem 0"><thead><tr><th>Tool MCP</th><th>Propósito</th></tr></thead><tbody>';
        e.tools.forEach(function(t){{h+='<tr><td><code>'+_esc2(t.name)+'</code></td><td>'+_esc2(t.purpose)+'</td></tr>';}});
        h+='</tbody></table></td></tr>';
      }}
    }});
    h+='</tbody></table>';
    box.innerHTML=h;
  }}).catch(function(){{_markOffline('api',false);}});
}}
function toggleMcpTools(row){{
  var next=row.nextElementSibling;
  if(next&&next.id==='mcp-tools-row') next.hidden=!next.hidden;
}}
/* 🔨 Proyecto — timeline de commits con porqué ARIS4U + comentarios + SSE */
var _projES=null;
var _projFacetsLoaded=false;
function _projClient(){{var el=document.getElementById('proj-client');return el?(el.value||'aris4u').trim():'aris4u';}}
function _loadProjClientList(){{
  if(_projFacetsLoaded) return;
  fetch('/memory/facets').then(function(r){{return r.json();}}).then(function(d){{
    if(!d.available) return;
    var dl=document.getElementById('proj-client-list');
    if(!dl) return;
    (d.clients||[]).forEach(function(c){{
      var o=document.createElement('option'); o.value=c; dl.appendChild(o);
    }});
    _projFacetsLoaded=true;
  }}).catch(function(){{}});
}}
function _esc2p(s){{var d=document.createElement('div');d.textContent=String(s||'');return d.innerHTML;}}
function _projWhyRow(why){{
  var parts=[];
  if(why&&why.decisions&&why.decisions.length)
    parts.push('<span class="proj-tag proj-dec" title="Decisión ARIS4U">D</span> '+why.decisions.map(function(d){{return _esc2p(String(d.decision||d.rationale||'decisión').slice(0,100));}}).join(' · '));
  if(why&&why.digests&&why.digests.length)
    parts.push('<span class="proj-tag proj-dig" title="Digest de sesión">S</span> '+why.digests.map(function(d){{return _esc2p(String(d.summary||d.date||'digest').slice(0,100));}}).join(' · '));
  if(why&&why.gates&&why.gates.length)
    parts.push('<span class="proj-tag proj-gate" title="Gate de calidad">G</span> '+why.gates.map(function(g){{return _esc2p(String((g.module_name||'gate')+(g.status?' ('+g.status+')':'')).slice(0,100));}}).join(' · '));
  return parts.length?'<div class="proj-why">'+parts.join('<br>')+'</div>':'';
}}
function _projCommentForm(sha, clt){{
  var escapedSha=_esc2p(sha), escapedClt=_esc2p(clt);
  return '<div class="proj-cform" id="cf-'+escapedSha+'">'
    +'<input class="search" style="width:100px" placeholder="Autor" id="cf-au-'+escapedSha+'" value="user-a">'
    +'<input class="search" style="width:260px;margin-left:.4rem" placeholder="Comentario…" id="cf-bo-'+escapedSha+'">'
    +'<button class="tbtn" style="margin-left:.4rem" data-sha="'+escapedSha+'" data-clt="'+escapedClt+'" onclick="postCommentBtn(this)">Enviar</button>'
    +'</div>';
}}
function _projCommitHtml(entry, clt){{
  var sha=entry.sha||'';
  var files=(entry.files||[]).slice(0,5).map(function(f){{return '<code>'+_esc2p(f)+'</code>';}}).join(' ');
  var moreFiles=entry.files&&entry.files.length>5?' <span class=hint>+'+( entry.files.length-5)+'</span>':'';
  return '<div class="proj-commit" id="pc-'+_esc2p(sha)+'">'
    +'<div class="proj-sha">'
    +'<span class="proj-sha-badge">'+_esc2p(sha.slice(0,7))+'</span>'
    +' <b>'+_esc2p(entry.subject)+'</b>'
    +'</div>'
    +'<div class="proj-meta hint">'+_esc2p(entry.author)+' · '+_esc2p((entry.date||'').slice(0,16))+'</div>'
    +(files?'<div class="proj-files">'+files+moreFiles+'</div>':'')
    +_projWhyRow(entry.why)
    +'<div class="proj-comments" id="pcm-'+_esc2p(sha)+'"><span class=hint>Cargando comentarios…</span></div>'
    +_projCommentForm(sha,clt)
    +'</div>';
}}
function loadProyectoComments(sha, clt){{
  var el=document.getElementById('pcm-'+sha);
  if(!el)return;
  fetch('/project/comments?client='+encodeURIComponent(clt)+'&sha='+encodeURIComponent(sha))
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(!d.available){{el.innerHTML='<span class=hint>sin comentarios</span>';return;}}
      var cs=d.comments||[];
      if(!cs.length){{el.innerHTML='<span class=hint>Sin comentarios aún.</span>';return;}}
      el.innerHTML=cs.map(function(c){{
        return '<div class="proj-cm"><span class="proj-cm-author">'+_esc2p(c.author)
          +(c.role?' <span class=hint>['+_esc2p(c.role)+']</span>':'')+'</span>'
          +' <span class="proj-cm-body">'+_esc2p(c.body)+'</span>'
          +' <span class=hint style="margin-left:.4rem">'+_esc2p((c.created_at||'').slice(0,16))+'</span>'
          +'</div>';
      }}).join('');
    }})
    .catch(function(){{if(el)el.innerHTML='<span class=hint>(comentarios no disponibles)</span>';}});
}}
function postCommentBtn(btn){{
  var sha=btn.dataset.sha, clt=btn.dataset.clt;
  postComment(sha, clt);
}}
function postComment(sha, clt){{
  var auEl=document.getElementById('cf-au-'+sha);
  var boEl=document.getElementById('cf-bo-'+sha);
  if(!auEl||!boEl)return;
  var author=auEl.value.trim()||'anon';
  var body=boEl.value.trim();
  if(!body){{boEl.focus();return;}}
  fetch('/project/comment',{{method:'POST',body:JSON.stringify({{sha:sha,author:author,role:'dev',body:body,client:clt}})}})
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(d.ok){{boEl.value='';loadProyectoComments(sha,clt);}}
      else{{alert('Error al enviar: '+(d.error||'?'));}}
    }})
    .catch(function(){{alert('Servidor no disponible.');}});
}}
function _projBuildHtml(b){{
  /* Renders ONE active build run as an ephemeral console block. */
  var repo=_esc2p(b.repo_path||'?');
  var since=_esc2p((b.started_at||'').slice(0,19).replace('T',' '));
  var lines=(b.log_tail||[]);
  var logHtml=lines.length
    ?'<pre class="proj-log">'+lines.map(function(l){{return _esc2p(l);}}).join('&#10;')+'</pre>'
    :'<p class=hint style="margin:.2rem 0">(log vacío o no disponible aún)</p>';
  return '<div class="proj-build-run">'
    +'<span class="proj-build-repo">'+repo+'</span>'
    +' <span class=hint>desde '+since+'</span>'
    +logHtml
    +'</div>';
}}
function _projInProgressHtml(builds){{
  if(!builds||!builds.length)return '';
  return '<div class="proj-in-progress" id="proj-building">'
    +'<div class="proj-build-header">'
    +'<span class="proj-build-badge">EN CURSO</span>'
    +' Construyéndose ahora'
    +' <span class=hint style="font-weight:400;font-size:.8rem"> — la verdad son los commits abajo</span>'
    +'</div>'
    +builds.map(_projBuildHtml).join('')
    +'</div>';
}}
function loadProyecto(skipStream){{
  _loadProjClientList();
  var clt=_projClient();
  var box=document.getElementById('proj-timeline');
  if(!box)return;
  box.innerHTML='<p class=hint>Cargando timeline…</p>';
  document.getElementById('proj-state').textContent='';
  _markOffline('proyecto',true);
  fetch('/project?client='+encodeURIComponent(clt))
    .then(function(r){{return r.json();}})
    .then(function(d){{
      if(!d.available){{box.innerHTML='<p class=hint>'+_esc2(d.reason||'no disponible')+'</p>';_markOffline('proyecto',false);return;}}
      var tl=d.timeline||[];
      var ip=d.in_progress||[];
      var html=_projInProgressHtml(ip);
      if(!tl.length){{
        box.innerHTML=html+'<p class=hint>Sin commits para este cliente.</p>';
        _markOffline('proyecto',false);
        if(!skipStream) startProyectoStream(clt);
        return;
      }}
      html+=tl.map(function(e){{return _projCommitHtml(e,clt);}}).join('');
      box.innerHTML=html;
      tl.forEach(function(e){{loadProyectoComments(e.sha,clt);}});
      if(!skipStream) startProyectoStream(clt);
    }})
    .catch(function(){{box.innerHTML='<p class=hint>Servidor no disponible.</p>';_markOffline('proyecto',false);}});
}}
function startProyectoStream(clt){{
  if(_projES){{_projES.close();_projES=null;}}
  if(typeof EventSource==='undefined')return;
  _projES=new EventSource('/project/stream?client='+encodeURIComponent(clt));
  var st=document.getElementById('proj-state'); if(st)st.textContent='● en vivo';
  _projES.onmessage=function(ev){{
    try{{
      var e=JSON.parse(ev.data);
      // Refresh sin reabrir el stream (evita el bucle onmessage→loadProyecto→startProyectoStream).
      loadProyecto(true);
    }}catch(_e){{}}
  }};
  _projES.onerror=function(){{
    var s=document.getElementById('proj-state');if(s)s.textContent='(stream cerrado)';
    stopProyectoStream();
  }};
}}
function stopProyectoStream(){{if(_projES){{_projES.close();_projES=null;var s=document.getElementById('proj-state');if(s&&s.textContent==='● en vivo')s.textContent='';}}}}
/* Estado es la sección activa al cargar → poblarla sin esperar un clic. */
loadStatus();
</script></body></html>"""


if __name__ == "__main__":
    raise SystemExit(main())
