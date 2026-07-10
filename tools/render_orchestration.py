#!/usr/bin/env python3
"""Render orchestration_map.json → HTML legible (mapa de orquestación ARIS4U).

Uso: python3 render_orchestration.py [salida.html]
Default salida: ~/Desktop/ARIS4U-ORQUESTACION.html
"""
import json
import sys
from pathlib import Path
from html import escape

HERE = Path(__file__).resolve().parent
DATA = HERE / "orchestration_map.json"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Desktop" / "ARIS4U-ORQUESTACION.html"

EXEC_COLOR = {
    "claude": ("#7c3aed", "🧠", "CLAUDE"),
    "augmenta": ("#2563eb", "🔌", "AUGMENTA"),
    "intercepta": ("#d97706", "🛡️", "INTERCEPTA"),
    "mecanico_local": ("#475569", "⚙️", "MECÁNICO"),
    "modelo_local": ("#dc2626", "🤖", "MODELO LOCAL"),
}
NATIVEZ_BADGE = {
    "nativo": ("#16a34a", "nativo"),
    "pegado": ("#ca8a04", "pegado"),
    "redundante": ("#9333ea", "redundante"),
    "mejor_en_claude": ("#dc2626", "mejor en Claude"),
}
DEP_LABEL = {"antes": "▶ antes", "despues": "◀ después", "bloquea": "⛔ bloquea", "independiente": "◦ side-effect"}


def _aggregate(events: list) -> tuple[dict, dict]:
    """Aggregate executor counts and nativez findings across all steps."""
    findings: dict = {"mejor_en_claude": [], "redundante": [], "pegado": []}
    counts: dict = {}
    for ev in events:
        for s in ev["steps"]:
            ex = s.get("executor", "mecanico_local")
            counts[ex] = counts.get(ex, 0) + 1
            nz = s.get("nativez", "nativo")
            if nz in findings:
                findings[nz].append((ev["event"], s))
    return findings, counts


def _render_step_row(s: dict) -> str:
    """Render a single <tr> for one step in the event table."""
    ex = s.get("executor", "mecanico_local")
    c, emo, lbl = EXEC_COLOR[ex]
    nz = s.get("nativez", "nativo")
    nzc, nzl = NATIVEZ_BADGE.get(nz, ("#16a34a", nz))
    conc = s.get("concurrency", "secuencial")
    conc_disp = (
        "<span class='tag' style='background:#a855f7'>∥ paralelo</span>"
        if conc == "paralelo"
        else "<span style='color:var(--mut);font-size:11px'>↓ seq</span>"
    )
    blkd = " <span class='block'>⛔</span>" if s.get("blocking") else ""
    why = s.get("why", "")
    why_html = f"<div class='why'>{escape(why)}</div>" if why else ""
    return (
        f"<tr><td class='n'>{s['n']}</td>"
        f"<td>{escape(s['name'])}{blkd}<span class='fl'>{escape(s['file_line'])}</span>{why_html}</td>"
        f"<td><span class='tag' style='background:{c}'>{emo} {lbl}</span></td>"
        f"<td>{conc_disp}</td>"
        f"<td><span class='badge' style='background:{nzc}'>{nzl}</span></td></tr>"
    )


def _render_event_block(ev: dict) -> list[str]:
    """Render the full HTML for one event block (details/summary + table)."""
    dep = ev.get("dep_claude", "")
    blk = " · <span class='block'>puede BLOQUEAR</span>" if ev.get("blocking") else ""
    rows = [
        "<details class='ev' open><summary class='ev-h'>",
        f"<span class='name'>{escape(ev['event'])}</span>",
        f"<span class='tag' style='background:#334155'>{DEP_LABEL.get(dep, dep)}</span>{blk}",
        f"<span class='dep'>{len(ev['steps'])} pasos</span></summary>",
        "<div class='ev-b'>",
        f"<div class='ev-sum'>{escape(ev.get('summary', ''))}</div>",
        f"<div class='note' style='font-style:normal;color:#7dd3fc'>Disparador: {escape(ev.get('trigger', ''))}</div>",
        "<table><tr><th>#</th><th>Paso</th><th>Ejecutor</th><th>Conc.</th><th>Nativez</th></tr>",
    ]
    for s in ev["steps"]:
        rows.append(_render_step_row(s))
    rows.append("</table>")
    for pb in ev.get("parallel_blocks", []):
        rows.append(f"<div class='par'>∥ <b>Bloque paralelo:</b> {escape(pb['desc'])}</div>")
    if ev.get("notes"):
        rows.append(f"<div class='note'>📌 {escape(ev['notes'])}</div>")
    rows.append("</div></details>")
    return rows


def _render_findings_section(findings: dict) -> list[str]:
    """Render the findings panel (mejor_en_claude / redundante / pegado)."""
    titles = {
        "mejor_en_claude": "🤖→🧠 Mejor en Claude (lo hace un modelo local débil / código)",
        "redundante": "♻️ Redundante (Claude / Claude Code ya lo hace nativo)",
        "pegado": "🩹 Pegado (bolted-on: funciona pero suple una limitación)",
    }
    parts: list[str] = ["<h2>🎯 Hallazgos — dónde NO es 100% nativo</h2>"]
    for key in ["mejor_en_claude", "redundante", "pegado"]:
        items = findings[key]
        if not items:
            continue
        parts.append(
            f"<div class='panel'><b>{escape(titles[key])}</b> <span style='color:var(--mut)'>({len(items)})</span>"
        )
        for ev, s in items:
            why = s.get("why", "")
            why_html = f"<div class='why'>{escape(why)}</div>" if why else ""
            parts.append(
                f"<div class='f-item'><b>{escape(ev)}</b> · {escape(s['name'])}"
                f"<span class='fl'>{escape(s['file_line'])}</span>{why_html}</div>"
            )
        parts.append("</div>")
    return parts


def _render_legends_kpis(
    counts: dict, events: list, findings: dict
) -> tuple[list[str], int, int]:
    """Render executor/nativez legends and KPI grid."""
    total_steps = sum(len(e["steps"]) for e in events)
    n_block = sum(1 for e in events for s in e["steps"] if s.get("blocking"))
    n_par = sum(1 for e in events for s in e["steps"] if s.get("concurrency") == "paralelo")
    n_non_native = len(findings["mejor_en_claude"]) + len(findings["redundante"])

    parts: list[str] = ["<div class='legend'>"]
    for k, (c, emo, lbl) in EXEC_COLOR.items():
        n = counts.get(k, 0)
        parts.append(f"<span class='chip' style='background:{c}'>{emo} {lbl} <b style='opacity:.7'>{n}</b></span>")
    parts.append("</div><div class='legend'>")
    for k, (c, lbl) in NATIVEZ_BADGE.items():
        parts.append(f"<span class='chip' style='background:{c};font-size:11px'>{lbl}</span>")
    parts.append("</div>")

    parts += [
        "<div class='kpis'>",
        "<div class='kpi'><b>7</b><span>eventos nativos</span></div>",
        f"<div class='kpi'><b>{total_steps}</b><span>pasos mapeados</span></div>",
        f"<div class='kpi'><b>{n_block}</b><span>pasos que BLOQUEAN</span></div>",
        f"<div class='kpi'><b>{n_par}</b><span>pasos paralelos (proceso OS)</span></div>",
        f"<div class='kpi'><b>{n_non_native}</b><span>hallazgos no-nativos</span></div>",
        "</div>",
    ]
    return parts, total_steps, n_non_native


def render() -> None:
    d = json.loads(DATA.read_text())
    meta = d["_meta"]
    events = d["events"]

    findings, counts = _aggregate(events)

    parts: list[str] = []
    parts.append(f"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(meta['title'])}</title>
<style>
:root{{--bg:#0f172a;--card:#1e293b;--card2:#273449;--txt:#e2e8f0;--mut:#94a3b8;--line:#334155}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--txt);font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:32px 20px 80px}}
h1{{font-size:26px;margin:0 0 4px}}h2{{font-size:20px;margin:36px 0 12px;border-bottom:1px solid var(--line);padding-bottom:8px}}
.sub{{color:var(--mut);margin:0 0 24px;font-size:13px}}
.legend{{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}}
.chip{{display:inline-flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:600;color:#fff}}
.panel{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin:14px 0}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin:16px 0}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;text-align:center}}
.kpi b{{display:block;font-size:24px}}.kpi span{{color:var(--mut);font-size:12px}}
.ev{{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:16px 0;overflow:hidden}}
.ev-h{{padding:16px 20px;background:var(--card2);cursor:pointer;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.ev-h .name{{font-size:18px;font-weight:700}}
.ev-h .dep{{font-size:12px;color:var(--mut);margin-left:auto}}
.ev-b{{padding:6px 20px 18px}}
.ev-sum{{color:var(--mut);font-size:13.5px;margin:10px 0 14px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:var(--mut);font-weight:600;padding:6px 8px;border-bottom:1px solid var(--line);font-size:11px;text-transform:uppercase;letter-spacing:.04em}}
td{{padding:7px 8px;border-bottom:1px solid #2a3a52;vertical-align:top}}
tr:hover td{{background:#22304a}}
.n{{color:var(--mut);width:24px}}
.fl{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:#7dd3fc;white-space:nowrap}}
.badge{{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:700;color:#fff}}
.tag{{display:inline-block;padding:1px 7px;border-radius:5px;font-size:10.5px;font-weight:700;color:#fff}}
.par{{background:#3b1d5e;border-left:3px solid #a855f7;padding:8px 12px;border-radius:6px;margin:10px 0;font-size:12.5px}}
.note{{color:var(--mut);font-size:12px;margin-top:10px;font-style:italic}}
.why{{color:var(--mut);font-size:11.5px;margin-top:2px}}
.block{{color:#fca5a5;font-weight:700}}
.f-item{{padding:8px 0;border-bottom:1px solid #2a3a52;font-size:13px}}
.f-item .fl{{margin-left:8px}}
details>summary{{list-style:none}}details>summary::-webkit-details-marker{{display:none}}
code{{background:#0b1220;padding:1px 5px;border-radius:4px;font-size:12px}}
</style></head><body><div class="wrap">""")

    parts.append(f"<h1>{escape(meta['title'])}</h1>")
    parts.append(f"<p class='sub'>Generado {escape(meta['generated'])} · {escape(meta['method'])}</p>")

    parts.append(f"<div class='panel'><b>⏱️ Realidad de concurrencia</b><br><span style='color:var(--mut);font-size:13.5px'>{escape(meta['concurrency_reality'])}</span></div>")
    parts.append(f"<div class='panel'><b>🔄 Ciclo de vida</b><br><code>{escape(meta['lifecycle'])}</code></div>")

    legend_kpi_parts, total_steps, n_non_native = _render_legends_kpis(counts, events, findings)
    parts.extend(legend_kpi_parts)

    parts.extend(_render_findings_section(findings))

    parts.append("<h2>📋 Orquestación por evento</h2>")
    for ev in events:
        parts.extend(_render_event_block(ev))

    parts.append("</div></body></html>")
    OUT.write_text("".join(parts))
    print(f"OK → {OUT}  ({OUT.stat().st_size//1024} KB, {total_steps} pasos, {n_non_native} hallazgos no-nativos)")


if __name__ == "__main__":
    render()
