"""Auto-verificación de salud de la Live Console — la metodología de auditoría, AUTOMATIZADA.

Corre el "gate MECÁNICO" que en la sesión 2026-06-29 se hizo a mano (smoke de endpoints, conteos
vs DB viva, coherencia ``/config``↔``/cap/mcp``, MCP duplicados/muertos, hooks con actividad,
huérfanos, drift de inventario). Cada check compara lo que la consola MUESTRA contra la VERDAD VIVA
del sistema — porque "100% funcional" = el espejo refleja un ARIS4U sano, no solo que el código corre.

Emite un reporte verde/rojo por check y un exit code (0 = todo verde, 1 = algún fallo). Así
re-verificar "¿todo funcional?" cuesta un comando en vez de re-derivar el proceso entero.

    python3 -m aris4u_console.selfcheck          # contra el server vivo en :8787
    python3 -m aris4u_console.selfcheck --json   # salida JSON (para CI / la propia consola)

Es el gate MECÁNICO (reproducible, sin LLM). Complementa —no reemplaza— al gate ADVERSARIAL de
juicio (workflow ``gate-consola``, lentes que intentan refutar el 100%). Metodología completa:
ver la memoria ``feedback_console_audit_methodology``.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from aris4u_console import live_data

BASE_URL = "http://127.0.0.1:8787"
_HOST = "127.0.0.1:8787"

# Endpoints GET que deben responder 200 con payload no trivial (el contrato vivo).
_ENDPOINTS = [
    "/", "/manifest", "/status", "/inventory.json", "/atoms", "/memory", "/memory/facets",
    "/telemetry", "/hooks", "/amplifier", "/config", "/quality", "/cap/skills", "/cap/agents",
    "/cap/mcp", "/cap/api", "/valorizacion", "/auditoria", "/backlog", "/skeletons", "/briefs",
]
_MIN_BYTES = 50  # un payload por debajo de esto es "vacío" sospechoso


@dataclass
class Check:
    """Un check de salud: nombre, veredicto y la evidencia (shown vs live)."""

    name: str
    ok: bool
    detail: str


def _get(path: str, base: str = BASE_URL) -> tuple[int, bytes]:
    """GET con Host header (la consola valida anti-DNS-rebinding). Devuelve (status, body)."""
    req = urllib.request.Request(base + path, headers={"Host": _HOST})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 (localhost fijo)
        return r.status, r.read()


def _get_json(path: str, base: str = BASE_URL) -> dict:
    """GET que parsea JSON ({} si no es JSON)."""
    _, body = _get(path, base)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}


def _scalar(db: Path, sql: str) -> int:
    """COUNT(*) read-only contra la DB viva (mode=ro, tolerante a fallo → -1)."""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            return int(con.execute(sql).fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return -1


# --- Checks individuales (cada uno devuelve un Check con evidencia) ---

def check_endpoints(base: str) -> list[Check]:
    """Todos los endpoints GET responden 200 con payload no vacío."""
    out: list[Check] = []
    for ep in _ENDPOINTS:
        try:
            status, body = _get(ep, base)
            ok = status == 200 and len(body) >= _MIN_BYTES
            out.append(Check(f"endpoint {ep}", ok, f"HTTP {status}, {len(body)} bytes"))
        except (urllib.error.URLError, OSError) as e:
            out.append(Check(f"endpoint {ep}", False, f"error: {e}"))
    return out


def check_memory_counts(base: str, repo: Path) -> list[Check]:
    """Los conteos que /memory MUESTRA == verdad viva de sessions.db (caza drift de datos)."""
    db = repo / live_data._DB
    shown = _get_json("/memory", base).get("totals", {})
    live = {
        # FIX #2: usar live_data._REAL (fuente única) en vez de duplicar la condición SQL.
        # Antes el string estaba hardcodeado aquí → podía derivar si _REAL cambiaba.
        "decisions": _scalar(db, f"SELECT COUNT(*) FROM decisions WHERE {live_data._REAL}"),
        "guards": _scalar(db, "SELECT COUNT(*) FROM guards"),
        "digests": _scalar(db, "SELECT COUNT(*) FROM digests"),
    }
    out: list[Check] = []
    for k, lv in live.items():
        sv = shown.get(k)
        out.append(Check(f"memory.{k} shown==live", sv == lv and lv >= 0,
                         f"mostrado={sv} vivo={lv}"))
    return out


def check_config_health(base: str) -> list[Check]:
    """/config: sin MCP duplicados y coherente con /cap/mcp (los globales aparecen en ambos)."""
    cfg = _get_json("/config", base)
    cap = _get_json("/cap/mcp", base)
    cap_names = {it.get("name") for g in cap.get("groups", []) for it in g.get("items", [])}
    glob = set(cfg.get("mcp_global", []))
    missing = glob - cap_names
    return [
        Check("config sin MCP duplicados", cfg.get("mcp_duplicated") == [],
              f"duplicated={cfg.get('mcp_duplicated')}"),
        Check("config↔cap/mcp coherentes", not missing,
              f"globales ausentes en cap/mcp: {sorted(missing) or 'ninguno'}"),
        Check("config tiene mcp_by_source", bool(cfg.get("mcp_by_source")),
              f"{len(cfg.get('mcp_by_source', []))} servers con origen"),
    ]


def check_hooks_activity(base: str) -> Check:
    """Al menos un evento del ciclo de vida muestra disparos (el cruce handler→evento funciona)."""
    hooks = _get_json("/hooks", base)
    active = [e["event"] for e in hooks.get("events", []) if e.get("count", 0) > 0]
    return Check("hooks con actividad cruzada", bool(active),
                 f"eventos con disparos: {active or 'NINGUNO (cruce roto?)'}")


def check_no_orphan(console_repo: Path) -> Check:
    """No existe la DB huérfana console/data/sessions.db (que confundiría al lector)."""
    orphan = console_repo / "data" / "sessions.db"
    return Check("sin DB huérfana en console/data", not orphan.exists(),
                 f"{orphan} {'EXISTE (archívala)' if orphan.exists() else 'ausente'}")


def check_inventory_served(base: str) -> Check:
    """El inventario servido está bien formado: totals.components == len(components)."""
    inv = _get_json("/inventory.json", base)
    total = inv.get("totals", {}).get("components")
    n = len(inv.get("components", []))
    return Check("inventario servido coherente", total == n and n > 0,
                 f"totals={total} len(components)={n}")


# --- Checks nuevos (bugs clase-A resueltos 2026-06-29) --------------------------------

def check_memory_search_no_provenance(base: str) -> Check:
    """FIX #1: /memory/search por defecto NO devuelve fact/provenance (solo _REAL).

    La función de búsqueda se inundaba de ruido antes del fix; ahora por defecto
    aplica el filtro _REAL (mem_type IS NULL OR NOT IN ('provenance','fact')).
    """
    d = _get_json("/memory/search?q=&limit=100", base)
    bad_types = {m for dec in d.get("decisions", [])
                 for m in [dec.get("mem_type")]
                 if m in ("provenance", "fact")}
    return Check("memory/search excluye fact/provenance por defecto",
                 not bad_types,
                 f"tipos encontrados: {sorted(bad_types) or 'ninguno — OK'}")


def check_amplifier_calls_nonzero(base: str, repo: Path) -> Check:
    """FIX #7 (round 2): usa live_data._F1_TOOLS (no hardcoded) y OSError explícito.

    Antes: herramientas hardcoded ("aris_structure","aris_critique") y OSError silencioso
    hacían que la comparación 0==0 pasara vacuamente sin detectar nada real.
    Ahora: usa live_data._F1_TOOLS como fuente única; OSError devuelve resultado explícito;
    log sin F1 calls se reporta como 'OK vacuo' (no como verificación real).
    """
    amp = _get_json("/amplifier", base)
    calls_shown = amp.get("calls", 0)
    events_path = repo / live_data._EVENTS
    live_calls = 0
    log_read_ok = False
    if events_path.is_file():
        try:
            for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    e = json.loads(line)
                    if e.get("event") == "mcp_tool" and e.get("tool") in live_data._F1_TOOLS:
                        live_calls += 1
                except (ValueError, json.JSONDecodeError):
                    pass
            log_read_ok = True
        except OSError as err:
            return Check("amplifier calls == log real", True,
                         f"OSError leyendo log — no verificable: {str(err)[:80]}")
    if not log_read_ok or live_calls == 0:
        # 0==0 no confirma nada útil; reportar como OK vacuo en vez de pasar silencioso
        return Check("amplifier calls == log real",
                     calls_shown == 0,
                     f"log sin F1 calls (log_read={'ok' if log_read_ok else 'no-file'}) "
                     f"consola={calls_shown} — OK vacuo")
    return Check("amplifier calls == log real", calls_shown == live_calls,
                 f"consola={calls_shown} log={live_calls}"
                 + ("" if calls_shown == live_calls else " ← DRIFT (leer log completo?)"))


def check_mcp_health_no_remote_fail(base: str) -> Check:
    """FIX #3: /cap/test/mcp no marca remotos como FAIL por binario.

    Stripe y cloudflare-builds son conectores HTTP (command=''); antes se marcaban
    FAIL por 'binario no encontrado'. Ahora se marcan ok con 'sin binario (remoto)'.

    FIX #8 (round 2): elimina condición 1 (rama muerta). Después del Fix #3, remotos
    tienen ok=True → 'not ok AND sin-binario-remoto' es imposible. Solo queda la
    condición 2: detecta la regresión cuando un server con command='' vuelve a evaluarse
    por binario y produce 'binario \\'\\' no encontrado' con ok=False.
    """
    d = _get_json("/cap/test/mcp", base)
    # Condición 2 (única activa): server con command vacío evaluado erróneamente por binario.
    remote_false_fail = [r["name"] for r in d.get("results", [])
                         if not r.get("ok") and r.get("detail", "").startswith("binario ''")]
    return Check("cap/test/mcp remotos no marcados FAIL",
                 not remote_false_fail,
                 f"remotos con FAIL indebido: {remote_false_fail or 'ninguno — OK'}")


def check_hooks_window_exposed(base: str) -> Check:
    """FIX #5: /hooks expone el tamaño de ventana (window_lines)."""
    d = _get_json("/hooks", base)
    wl = d.get("window_lines")
    w = d.get("window")
    ok = wl is not None and w is not None
    return Check("hooks expone window_lines y window", ok,
                 f"window_lines={wl} window={w}" + (" — OK" if ok else " ← falta (bug #5)"))


def check_config_caps_mcp_same_set(base: str) -> Check:
    """Endurece Bug #1: /config y /cap/mcp listan el mismo conjunto de MCP.

    /config.mcp_global ∪ mcp_repo debe ser igual al conjunto de nombres de items en
    /cap/mcp. Cualquier diferencia revela que _discover_mcps y read_mcp divergen.
    """
    cfg = _get_json("/config", base)
    cap = _get_json("/cap/mcp", base)
    cap_names = {it.get("name") for g in cap.get("groups", []) for it in g.get("items", [])}
    cfg_names = set(cfg.get("mcp_global", [])) | set(cfg.get("mcp_repo", []))
    missing_in_cfg = cap_names - cfg_names
    missing_in_cap = cfg_names - cap_names
    ok = not missing_in_cfg and not missing_in_cap
    detail = ("conjuntos iguales — OK" if ok else
              f"en cap/mcp no en config: {sorted(missing_in_cfg) or '∅'} | "
              f"en config no en cap/mcp: {sorted(missing_in_cap) or '∅'}")
    return Check("config y cap/mcp mismo conjunto MCP", ok, detail)


def check_recent_decisions_real(base: str) -> Check:
    """Endurece Bug #4: recent_decisions de /memory no contiene entradas provenance/fact.

    Tras el Fix #4 (round 2), la query de recent_decisions lleva WHERE _REAL. Este check
    detecta si vuelven a colarse commits ([commit …]) u otros provenance en los recientes.
    """
    mem = _get_json("/memory", base)
    # Proxy fiable: commits tienen decision que empieza con '[commit '
    bad = [(d.get("decision") or "")[:60] for d in mem.get("recent_decisions", [])
           if (d.get("decision") or "").startswith("[commit")]
    return Check("recent_decisions sin provenance",
                 not bad,
                 f"commits en recientes: {bad or 'ninguno — OK'}")


def check_inventory_fresh_vs_head(base: str, repo: Path) -> Check:
    """Endurece Bug #5: inventory.json git.head == HEAD real del repo.

    El inventario se genera al arrancar; si hay commits desde el último arranque,
    git.head queda 1 commit detrás. Este check detecta el stale y recuerda reiniciar.
    """
    import subprocess
    inv = _get_json("/inventory.json", base)
    inv_head = (inv.get("git") or {}).get("head", "")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
        actual = result.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return Check("inventory git.head == HEAD", True,
                     "git no disponible — no verificable")
    ok = bool(inv_head and actual and inv_head == actual)
    return Check("inventory git.head == HEAD", ok,
                 f"inventory={inv_head!r} HEAD={actual!r}"
                 + (" — OK" if ok else " ← stale (reinicia el server)"))


def check_briefs_from_live_table(base: str) -> Check:
    """Endurece el fix de /briefs: lee observations (viva), no session_summaries (congelada).

    El plugin claude-mem migró de ``session_summaries`` (congelada el 2026-05-18) a
    ``observations`` en mayo-2026. Si la fecha más reciente de los briefs es <= esa fecha,
    la consola volvió a leer la tabla muerta y muestra contexto stale de mayo.
    """
    d = _get_json("/briefs", base)
    latest = max((b.get("created_at", "") for b in d.get("briefs", [])), default="")
    ok = latest > "2026-05-18"
    return Check("briefs desde tabla viva (observations)", ok,
                 f"más reciente={latest[:10] or '∅'}" + (" — OK" if ok else " ← tabla congelada"))


def _mcp_false_stdio(items: list[dict]) -> list[str]:
    """Nombres de items con kind=mcp-stdio pero desc con URL HTTP (síntoma del bug figma)."""
    return [
        it.get("name", "?")
        for it in items
        if it.get("kind") == "mcp-stdio"
        and "http" in (it.get("desc") or "").lower()
    ]


def _figma_kind_detail(items: list[dict]) -> tuple[bool, str]:
    """(ok, detail) para el caso concreto de figma: debe ser mcp-remote."""
    figma = next((it for it in items if it.get("name") == "figma"), None)
    if figma is None:
        return False, "figma ausente de /cap/mcp"
    kind = figma.get("kind")
    if kind == "mcp-remote":
        return True, "figma=mcp-remote — OK"
    return False, f"figma kind={kind!r} (esperado mcp-remote)"


def check_mcp_kind_remote_correct(base: str) -> Check:
    """FIX clase-A: MCPs con url/type-http salen como mcp-remote, no mcp-stdio.

    Antes de la corrección en _mcp_from_file + capabilities.py, servidores HTTP remotos
    (type=http, sin command) de plugins se clasificaban como mcp-stdio porque su URL se
    colapsaba en el campo command. Síntoma: figma→ kind='mcp-stdio', desc='stdio · https://…'.

    Detecta la regresión: ningún item debe tener kind=mcp-stdio con desc 'stdio · http…',
    y figma específicamente debe salir como mcp-remote.
    """
    cap = _get_json("/cap/mcp", base)
    items = [it for g in cap.get("groups", []) for it in g.get("items", [])]
    false_stdio = _mcp_false_stdio(items)
    figma_ok, figma_detail = _figma_kind_detail(items)
    ok = not false_stdio and figma_ok
    parts = []
    if false_stdio:
        parts.append(f"stdio falso con URL: {false_stdio}")
    parts.append(figma_detail)
    return Check("MCP http/url clasificados como mcp-remote", ok, "; ".join(parts))


def check_memory_domains_no_phantom(base: str) -> Check:
    """Fix #1 clase-A (7º gate): todo domain de /memory/facets devuelve >0 resultados.

    Antes del fix, ``memory_facets`` no aplicaba ``_REAL`` a la query de dominios, por lo que
    listaba 69 dominios incluyendo los de filas provenance/fact — que luego devolvían 0
    resultados en /memory/search porque la búsqueda sí aplica ``_REAL``. Tras el fix los
    dominios listados deben ser solo los buscables (cada uno devuelve ≥1 resultado).
    """
    facets = _get_json("/memory/facets", base)
    domains = facets.get("domains", [])
    phantom = []
    for d in domains[:40]:  # limitar a 40 para no sobrecargar el server en selfcheck
        result = _get_json(f"/memory/search?domain={d}&limit=1", base)
        if not result.get("decisions") and not result.get("guards"):
            phantom.append(d)
    ok = not phantom
    return Check(
        "memory/facets sin dominios fantasma",
        ok,
        f"dominios={len(domains)} fantasmas={phantom or 'ninguno — OK'}",
    )


def check_test_mcp_covers_caps(base: str) -> Check:
    """Fix #2 clase-A (7º gate): /cap/test/mcp cubre el mismo conjunto que /cap/mcp.

    Antes del fix, ``_health_mcp`` solo iteraba ``_local_mcp_servers()`` (6 servers) y omitía
    los MCPs de plugins (figma, shadcn, firebase, serena, mcp-search) y los connectors remotos
    vistos en telemetría (claude-in-chrome, Google Drive, Intuit QuickBooks, ide). Tras el fix
    ambos endpoints deben listar el mismo conjunto de nombres.
    """
    cap = _get_json("/cap/mcp", base)
    test = _get_json("/cap/test/mcp", base)
    cap_names = {it.get("name") for g in cap.get("groups", []) for it in g.get("items", [])}
    test_names = {r.get("name") for r in test.get("results", [])}
    missing = cap_names - test_names
    extra = test_names - cap_names
    ok = not missing and not extra
    detail = ("conjuntos iguales — OK" if ok else
              f"en cap/mcp no en test: {sorted(missing) or '∅'} | "
              f"en test no en cap/mcp: {sorted(extra) or '∅'}")
    return Check("cap/test/mcp cubre mismo conjunto que cap/mcp", ok, detail)


# --- Checks nuevos — endpoints críticos V18 (2026-07-07) ------------------------------

def check_routing_endpoint(base: str) -> Check:
    """/routing responde 200 con las claves del observatorio de routing V18.

    Las claves ``dispatches``, ``discipline_pct`` y ``by_model`` son el contrato mínimo
    de ``tools/cost_report.compute_report``. Si faltan, el observatorio está roto en
    silencio aunque el endpoint devuelva 200.
    """
    try:
        status, body = _get("/routing", base)
        if status != 200:
            return Check("routing HTTP 200 + claves V18", False,
                         f"HTTP {status} (esperado 200)")
        d: dict = {}
        try:
            d = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return Check("routing HTTP 200 + claves V18", False, "cuerpo no es JSON")
        missing = [k for k in ("dispatches", "discipline_pct", "by_model") if k not in d]
        ok = not missing
        return Check("routing HTTP 200 + claves V18", ok,
                     f"claves ausentes: {missing}" if missing else
                     f"dispatches={d.get('dispatches')} discipline={d.get('discipline_pct')}% — OK")
    except (urllib.error.URLError, OSError) as e:
        return Check("routing HTTP 200 + claves V18", False, f"error: {e}")


def check_project_timeline_endpoint(base: str) -> Check:
    """/project?client=aris4u responde 200 con available truthy y count presente.

    Verifica la superficie cowork: el endpoint de timeline del cliente ``aris4u`` debe
    estar disponible y devolver un conteo numérico (aunque sea 0 commits).
    No enviamos Origin → pasa el guard CSRF (igual que /memory en check_memory_counts).
    """
    try:
        status, body = _get("/project?client=aris4u", base)
        if status != 200:
            return Check("project?client=aris4u HTTP 200 + available", False,
                         f"HTTP {status} (esperado 200)")
        d: dict = {}
        try:
            d = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return Check("project?client=aris4u HTTP 200 + available", False,
                         "cuerpo no es JSON")
        available = bool(d.get("available"))
        has_count = "count" in d
        ok = available and has_count
        return Check("project?client=aris4u HTTP 200 + available", ok,
                     f"available={d.get('available')} count={d.get('count')}"
                     + (" — OK" if ok else " ← falta available o count"))
    except (urllib.error.URLError, OSError) as e:
        return Check("project?client=aris4u HTTP 200 + available", False, f"error: {e}")


def check_project_comments_endpoint(base: str, repo: Path) -> Check:
    """/project/comments?client=aris4u&sha=HEAD responde 200 con comments lista.

    Usa el HEAD real del repo para el sha (o 'HEAD' literal si git no está disponible).
    Con lista vacía basta; el contrato es que ``comments`` sea una lista, no un error.
    No enviamos Origin → pasa el guard CSRF.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
        sha = result.stdout.strip() or "HEAD"
    except (subprocess.SubprocessError, OSError):
        sha = "HEAD"
    url = f"/project/comments?client=aris4u&sha={sha}"
    try:
        status, body = _get(url, base)
        if status != 200:
            return Check("project/comments HTTP 200 + comments lista", False,
                         f"HTTP {status} sha={sha!r}")
        d: dict = {}
        try:
            d = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return Check("project/comments HTTP 200 + comments lista", False,
                         "cuerpo no es JSON")
        comments = d.get("comments")
        ok = isinstance(comments, list)
        return Check("project/comments HTTP 200 + comments lista", ok,
                     f"sha={sha!r} comments={'lista OK' if ok else repr(comments)[:60]}")
    except (urllib.error.URLError, OSError) as e:
        return Check("project/comments HTTP 200 + comments lista", False, f"error: {e}")


def check_phi_guard_blocks_endpoint(base: str) -> Check:
    """/phi-guard-blocks responde 200 (smoke check — endpoint de lectura existente)."""
    try:
        status, body = _get("/phi-guard-blocks", base)
        ok = status == 200 and len(body) > 0
        return Check("phi-guard-blocks HTTP 200", ok,
                     f"HTTP {status}, {len(body)} bytes"
                     + (" — OK" if ok else " ← fallo"))
    except (urllib.error.URLError, OSError) as e:
        return Check("phi-guard-blocks HTTP 200", False, f"error: {e}")


def run_selfcheck(base: str = BASE_URL, repo: Path | None = None,
                  console_repo: Path | None = None) -> list[Check]:
    """Corre todos los checks mecánicos y devuelve la lista de resultados.

    Args:
        base: URL del server vivo de la consola.
        repo: Raíz del motor ARIS4U (default ``live_data.DEFAULT_REPO``).
        console_repo: Raíz del repo de la consola (default: el padre de este paquete).

    Returns:
        Lista de :class:`Check`; un check con ``ok=False`` significa que el espejo no refleja
        la verdad viva (o el sistema tiene un problema que la consola correctamente revela).
    """
    repo = repo or live_data.DEFAULT_REPO
    console_repo = console_repo or Path(__file__).resolve().parent.parent
    checks: list[Check] = []
    checks += check_endpoints(base)
    checks += check_memory_counts(base, repo)
    checks += check_config_health(base)
    checks.append(check_hooks_activity(base))
    checks.append(check_inventory_served(base))
    checks.append(check_no_orphan(console_repo))
    # Checks — bugs clase-A 2026-06-29 (round 1)
    checks.append(check_memory_search_no_provenance(base))
    checks.append(check_amplifier_calls_nonzero(base, repo))
    checks.append(check_mcp_health_no_remote_fail(base))
    checks.append(check_hooks_window_exposed(base))
    checks.append(check_briefs_from_live_table(base))
    # Checks — bugs clase-A round 2 (endurece los fixes 1/4/5)
    checks.append(check_config_caps_mcp_same_set(base))
    checks.append(check_recent_decisions_real(base))
    checks.append(check_inventory_fresh_vs_head(base, repo))
    # Check — bug clase-A round 3: figma MCP kind correcto (2026-06-29)
    checks.append(check_mcp_kind_remote_correct(base))
    # Checks — bugs clase-A 7º gate adversarial (2026-06-29): endurecen fixes #1 y #2
    checks.append(check_memory_domains_no_phantom(base))
    checks.append(check_test_mcp_covers_caps(base))
    # Checks — endpoints críticos V18 sin cobertura previa (2026-07-07)
    checks.append(check_routing_endpoint(base))
    checks.append(check_project_timeline_endpoint(base))
    checks.append(check_project_comments_endpoint(base, repo))
    checks.append(check_phi_guard_blocks_endpoint(base))
    return checks


def main(argv: list[str] | None = None) -> int:
    """CLI: imprime el reporte y devuelve exit code (0 = todo verde)."""
    argv = argv if argv is not None else sys.argv[1:]
    as_json = "--json" in argv
    try:
        checks = run_selfcheck()
    except (urllib.error.URLError, OSError) as e:
        msg = f"No se pudo contactar la consola en {BASE_URL}: {e}. ¿Está corriendo el server?"
        print(json.dumps({"available": False, "reason": msg}) if as_json else f"✗ {msg}")
        return 2
    failed = [c for c in checks if not c.ok]
    if as_json:
        print(json.dumps({"available": True, "all_green": not failed,
                          "checks": [asdict(c) for c in checks]}, ensure_ascii=False))
    else:
        for c in checks:
            print(f"  {'✅' if c.ok else '❌'} {c.name:34} {c.detail}")
        print(f"\n{'✅ TODO VERDE' if not failed else f'❌ {len(failed)} de {len(checks)} FALLARON'}"
              f"  ({len(checks) - len(failed)}/{len(checks)} ok)")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
