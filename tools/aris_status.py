#!/usr/bin/env python3
"""Panel de status de capacidades de ARIS4U (read-only).

Motor reutilizable de la Capa 0 del wrapper (ARIS4U Desktop): lee el estado vivo
de la capa ARIS4U sin escribir jamás en nada de producción.

Fuentes:
  - ~/.claude/settings.json  -> hooks por evento + mcpServers + env (JSON puro).
  - data/sessions.db         -> conteos decisions/guards/digests por cliente (URI mode=ro).
  - logs/v16.1-events.jsonl  -> últimos eventos de telemetría (tail por seek).

Seguridad: sessions.db se abre SIEMPRE con `file:...?mode=ro` (uri=True). Aunque
hubiera un bug, SQLite rechaza cualquier escritura. Lección del audit V2 (los tests
corrompieron la DB real): este lector no puede tocarla.

Uso:
    python3 tools/aris_status.py            # panel coloreado
    python3 tools/aris_status.py --no-color
    python3 tools/aris_status.py --json     # salida estructurada (para Capa 1/2)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ARIS_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = Path.home() / ".claude" / "settings.json"
SESSIONS_DB = ARIS_ROOT / "data" / "sessions.db"
EVENTS_LOG = ARIS_ROOT / "logs" / "v16.1-events.jsonl"

GUARD_MARKERS = ("guard", "phi_", "migration_linter")


def _script_name(cmd: str) -> str:
    """Extrae el nombre del .sh de un comando tipo `bash "/ruta/x-guard.sh"`."""
    for tok in cmd.replace('"', " ").replace("'", " ").split():
        if tok.endswith(".sh"):
            return tok.rsplit("/", 1)[-1][:-3]
    return cmd.rsplit("/", 1)[-1].strip('"')


def _color(code: str, text: str, enabled: bool) -> str:
    """Envuelve `text` en un código ANSI si los colores están activos."""
    return f"\033[{code}m{text}\033[0m" if enabled else text


def load_settings() -> dict[str, Any]:
    """Lee ~/.claude/settings.json (JSON puro). Devuelve {} si falla."""
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def hook_summary(settings: dict[str, Any]) -> tuple[dict[str, int], list[str], int]:
    """Resume hooks por evento y cuenta cuántos son de ARIS4U.

    Returns:
        (conteo_por_evento, lista_guards, total_hooks)
    """
    by_event: dict[str, int] = {}
    guards: list[str] = []
    total = 0
    for event, entries in (settings.get("hooks") or {}).items():
        count = 0
        for entry in entries:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                count += 1
                total += 1
                if "aris4u" in cmd and any(m in cmd for m in GUARD_MARKERS):
                    guards.append(_script_name(cmd))
        if count:
            by_event[event] = count
    return by_event, sorted(set(guards)), total


def mcp_servers(settings: dict[str, Any]) -> list[str]:
    """Nombres de MCP servers cableados en settings global."""
    return sorted((settings.get("mcpServers") or {}).keys())


def db_counts() -> dict[str, Any]:
    """Conteos vivos de sessions.db en modo estrictamente read-only."""
    out: dict[str, Any] = {"ok": False}
    if not SESSIONS_DB.exists():
        return out
    uri = f"file:{SESSIONS_DB}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            cur = con.cursor()
            for tbl in ("decisions", "guards", "digests"):
                out[tbl] = cur.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
            out["by_client"] = cur.execute(
                "SELECT COALESCE(client_id,'(none)') c, count(*) "
                "FROM decisions GROUP BY c ORDER BY 2 DESC LIMIT 6"
            ).fetchall()
            out["ok"] = True
        finally:
            con.close()
    except sqlite3.Error as exc:
        out["error"] = str(exc)
    return out


def tail_events(n: int = 5) -> list[dict[str, Any]]:
    """Últimos `n` eventos del JSONL leyendo solo el final del archivo."""
    if not EVENTS_LOG.exists():
        return []
    try:
        size = EVENTS_LOG.stat().st_size
        with EVENTS_LOG.open("rb") as f:
            f.seek(max(0, size - 65536))
            chunk = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events[-n:]


def collect() -> dict[str, Any]:
    """Reúne todo el estado en una estructura (consumible por Capa 1/2)."""
    settings = load_settings()
    by_event, guards, total_hooks = hook_summary(settings)
    return {
        "version": _plugin_version(),
        "hooks": {"by_event": by_event, "total": total_hooks},
        "guards": guards,
        "mcp": mcp_servers(settings),
        "memory": db_counts(),
        "events": tail_events(),
        "env": {
            k: v
            for k, v in (settings.get("env") or {}).items()
            if k.startswith("ARIS4U") or k.startswith("CLAUDE_CODE")
        },
        "model_default": settings.get("model", "(no fijado → arranca en el default)"),
    }


def _plugin_version() -> str:
    """Versión del plugin desde .claude-plugin/plugin.json."""
    try:
        pj = json.loads((ARIS_ROOT / ".claude-plugin" / "plugin.json").read_text())
        return pj.get("version", "?")
    except (OSError, json.JSONDecodeError):
        return "?"


def render(data: dict[str, Any], color: bool) -> str:
    """Panel textual legible. Verde = vivo, amarillo = atención."""
    def ok(t: str) -> str:
        return _color("32", t, color)

    def warn(t: str) -> str:
        return _color("33", t, color)

    def dim(t: str) -> str:
        return _color("2", t, color)

    def head(t: str) -> str:
        return _color("1;36", t, color)

    L: list[str] = []
    L.append(head(f"ARIS4U v{data['version']} — STATUS DE CAPACIDADES"))
    L.append("")

    h = data["hooks"]
    L.append(f"  {ok('●')} HOOKS: {h['total']} cableados ({len(h['by_event'])} eventos)")
    for ev, n in h["by_event"].items():
        L.append(dim(f"      {ev}: {n}"))

    mcp = data["mcp"]
    L.append(f"  {ok('●')} MCP servers: {len(mcp)}  [{', '.join(mcp)}]")
    if "aris4u" in mcp:
        L.append(dim("      aris4u tools: aris_search · aris_recall_client · "
                     "aris_dialectic · aris_health · aris_ingest"))

    g = data["guards"]
    L.append(f"  {ok('●')} GUARDS: {len(g)}")
    if g:
        L.append(dim("      " + ", ".join(g)))

    m = data["memory"]
    if m.get("ok"):
        L.append(f"  {ok('●')} MEMORIA: "
                 f"{m['decisions']} decisions · {m['guards']} guards · {m['digests']} digests")
        clients = " · ".join(f"{c}:{n}" for c, n in m.get("by_client", []))
        L.append(dim(f"      por cliente: {clients}"))
    else:
        L.append(f"  {warn('●')} MEMORIA: sessions.db no legible {m.get('error','')}")

    md = data["model_default"]
    tag = ok(md) if str(md).startswith("claude-") else warn(md)
    L.append(f"  {ok('●')} MODELO por defecto: {tag}")

    ev = data["events"]
    if ev:
        L.append(f"  {ok('●')} ÚLTIMOS EVENTOS:")
        for e in ev:
            kind = e.get("event") or e.get("hook") or e.get("type") or "?"
            ts = (e.get("ts") or e.get("timestamp") or "")[:19]
            L.append(dim(f"      {ts}  {kind}"))

    return "\n".join(L)


def main(argv: list[str]) -> int:
    data = collect()
    if "--json" in argv:
        print(json.dumps(data, indent=2, default=str))
        return 0
    print(render(data, color="--no-color" not in argv and sys.stdout.isatty()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
