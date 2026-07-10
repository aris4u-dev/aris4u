"""Self-briefing automático de ARIS4U — inyectado al arrancar sesión (source==startup).

build_briefing() produce un bloque denso y accionable con:
  - identidad (1 línea fija)
  - auto-automático (hooks que corren solos) y opt-in (MCP tools + skills)
  - memoria viva (counts de sessions.db + vectores de aris_vectors.db)
  - hardware canónico
  - salud write-path (stale si última decisión > 48h)

Diseño de tolerancia a fallos:
  - Todas las fuentes son leídas en modo read-only / con timeout
  - Cualquier excepción devuelve "" (fail-open total: nunca rompe SessionStart)
  - Si len(bloque) > BUDGET_CHARS se degrada (by_client→top-3, se omite hardware)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

ARIS_ROOT = Path(__file__).resolve().parents[3]  # hooks/dispatch/events/ → aris4u/
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
SESSIONS_DB = ARIS_ROOT / "data" / "sessions.db"
VECTORS_DB = ARIS_ROOT / "data" / "aris_vectors.db"
PLUGIN_JSON = ARIS_ROOT / ".claude-plugin" / "plugin.json"

BUDGET_CHARS = 2200
IDENTITY_TMPL = (
    "amplificador de {owner} sobre Opus 4.8 · plugin ARIS4U v{ver} "
    "= memoria local + guards + trazabilidad por-cliente"
)
MCP_TOOLS = "aris_recall_client · aris_search · aris_ingest · aris_dialectic · aris_health"
# Bloque de hardware canónico (literal): se usa cuando build_hardware_block no está disponible.
_HARDWARE_BLOCK_LITERAL = (
    "  • M5 Pro 48GB unified + GPU Metal 20c MPS=PRIMARIO (no dejar ociosa la GPU)\n"
    "  • W2 (ssh w2) RTX 3070 8GB / worker ocupado — verificar RAM antes de despachar\n"
    "  • W1/W3/W4 MUERTOS — no referenciar ni despachar"
)


def _hardware_block_safe() -> str:
    """Obtiene el bloque de hardware vía engine.v16.config (fail-open).

    Solo llama a build_hardware_block si _USER_CFG contiene una clave "hardware"
    (es decir, el usuario configuró algo). Si no hay config o no hay clave "hardware",
    devuelve el literal canónico para preservar comportamiento idéntico al actual.

    Returns:
        Bloque de hardware multi-línea listo para incrustar en el briefing.
    """
    try:
        aris_root_str = str(ARIS_ROOT)
        if aris_root_str not in sys.path:
            sys.path.insert(0, aris_root_str)
        from engine.v16.config import _USER_CFG, build_hardware_block  # noqa: PLC0415
        if _USER_CFG.get("hardware"):
            return build_hardware_block(_USER_CFG)
    except Exception:
        pass
    return _HARDWARE_BLOCK_LITERAL


def _orchestration_posture_safe() -> str:
    """Postura de orquestación (Fase 3) para el briefing — instala la actitud una vez.

    Llama a ``tools.orchestration_protocol.build_session_posture`` (fail-open): si el
    inventario vivo no es legible o el módulo no está, devuelve "" y el briefing sigue
    igual. Reencuadra a Claude como orquestador de su propio toolkit sin nombres de
    cliente (genérico para cualquier instancia).

    Returns:
        Bloque corto de postura, o "" si no hay inventario/módulo (neutral).
    """
    try:
        aris_root_str = str(ARIS_ROOT)
        if aris_root_str not in sys.path:
            sys.path.insert(0, aris_root_str)
        from tools.orchestration_protocol import build_session_posture  # noqa: PLC0415
        return build_session_posture()
    except Exception:
        return ""


def _owner_safe() -> str:
    """Obtiene el nombre del dueño de la instancia desde config (fail-open).

    Returns:
        Nombre del dueño o 'el usuario' como fallback (para no romper el briefing existente).
    """
    try:
        aris_root_str = str(ARIS_ROOT)
        if aris_root_str not in sys.path:
            sys.path.insert(0, aris_root_str)
        from engine.v16.config import cfg_owner  # noqa: PLC0415
        return cfg_owner()
    except Exception:
        return "el usuario"


def _clients_safe() -> str:
    """Obtiene la lista de clientes activos como string (fail-open).

    Returns:
        String "Cliente1 / Cliente2 / ..." o "tu cliente activo" si lista vacía.
    """
    try:
        aris_root_str = str(ARIS_ROOT)
        if aris_root_str not in sys.path:
            sys.path.insert(0, aris_root_str)
        from engine.v16.config import cfg_clients  # noqa: PLC0415
        clients = cfg_clients()
        if clients:
            return " / ".join(clients)
    except Exception:
        pass
    return "tus clientes activos"


# ---------------------------------------------------------------------------
# Helpers de lectura (read-only, fail-open)
# ---------------------------------------------------------------------------

def _plugin_version() -> str:
    """Versión del plugin desde .claude-plugin/plugin.json.

    Returns:
        Cadena de versión (p.ej. "16.9.0") o "?" si no se puede leer.
    """
    try:
        data = json.loads(PLUGIN_JSON.read_text())
        return data.get("version", "?")
    except Exception:
        return "?"


def _load_settings() -> dict[str, Any]:
    """Lee ~/.claude/settings.json en modo lectura.

    Returns:
        Dict con el contenido de settings o {} ante cualquier error.
    """
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        return {}


def _count_pretool_hooks(settings: dict[str, Any]) -> tuple[int, int]:
    """Cuenta hooks PreToolUse y cuántos son guards (bloqueantes potenciales).

    Args:
        settings: Contenido de settings.json ya parseado.

    Returns:
        (total_pretool, guards_bloqueantes)
    """
    pretool_entries = settings.get("hooks", {}).get("PreToolUse", [])
    total = 0
    blocking = 0
    for entry in pretool_entries:
        for hook in entry.get("hooks", []):
            total += 1
            cmd = hook.get("command", "")
            if any(m in cmd for m in ("guard", "phi_guard", "migration_linter")):
                blocking += 1
    return total, blocking


def _db_memory() -> dict[str, Any]:
    """Cuenta decisiones/guards/digests y top clientes desde sessions.db (read-only).

    Returns:
        Dict con claves: ok, decisions, guards, digests, by_client, last_decision_age_h.
    """
    out: dict[str, Any] = {"ok": False}
    if not SESSIONS_DB.exists():
        return out
    uri = f"file:{SESSIONS_DB}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1.5)
        try:
            cur = con.cursor()
            out["decisions"] = cur.execute("SELECT count(*) FROM decisions").fetchone()[0]
            out["guards"] = cur.execute("SELECT count(*) FROM guards").fetchone()[0]
            out["digests"] = cur.execute("SELECT count(*) FROM digests").fetchone()[0]
            out["by_client"] = cur.execute(
                "SELECT COALESCE(client_id,'(none)') c, count(*) n "
                "FROM decisions GROUP BY c ORDER BY n DESC LIMIT 6"
            ).fetchall()
            # Edad de la última decisión para alerta write-path
            last_row = cur.execute("SELECT MAX(created_at) FROM decisions").fetchone()
            if last_row and last_row[0]:
                last = datetime.strptime(str(last_row[0])[:19], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=UTC
                )
                out["last_decision_age_h"] = (datetime.now(UTC) - last).total_seconds() / 3600
            else:
                out["last_decision_age_h"] = None
            out["ok"] = True
        finally:
            con.close()
    except sqlite3.Error as exc:
        out["error"] = str(exc)
    return out


def _vector_count() -> int:
    """Conteo de vectores desde aris_vectors.db via vec_map (sin extensión sqlite-vec).

    Returns:
        Número de vectores o 0 si la DB no existe / falla la consulta.
    """
    if not VECTORS_DB.exists():
        return 0
    uri = f"file:{VECTORS_DB}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1.5)
        try:
            n: int = con.execute("SELECT count(*) FROM vec_map").fetchone()[0]
            return n
        finally:
            con.close()
    except Exception:
        return 0


def _last_digest_ts(mem: dict[str, Any]) -> str:
    """Deduce la fecha del último digest desde la DB (si ok)."""
    if not mem.get("ok") or not SESSIONS_DB.exists():
        return "(no data)"
    uri = f"file:{SESSIONS_DB}?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=1.5)
        try:
            row = con.execute("SELECT MAX(created_at) FROM digests").fetchone()
            return str(row[0])[:16] if row and row[0] else "(no digests)"
        finally:
            con.close()
    except Exception:
        return "(error)"


# ---------------------------------------------------------------------------
# Builder principal
# ---------------------------------------------------------------------------

def build_briefing(source: str) -> str:  # noqa: ARG001 — futura expansión por source
    """Construye el bloque de self-briefing para inyectar en SessionStart.

    Solo debe llamarse cuando source == "startup". Si se llama para "resume" el
    caller NO debe llamar esta función (guardia en session_start.py).

    Args:
        source: Valor del campo "source" del evento SessionStart. Actualmente
            ignorado dentro de la función (el filtro está en el caller), pero se
            mantiene en firma para extensibilidad.

    Returns:
        Bloque de texto (< BUDGET_CHARS) listo para anteponer a additionalContext,
        o "" ante cualquier excepción (fail-open total).
    """
    try:
        return _build_briefing_inner()
    except Exception:
        return ""


def _phi_status(settings: dict[str, Any]) -> str:
    """Línea de estado PHI del briefing — siempre visible. OFF por defecto.

    ON solo si el switch global ``ARIS4U_HEALTHCARE=1`` o si el cwd está dentro de un
    proyecto cliente healthcare. Sin esto, PHI no actúa (off-by-default 2026-06-22).
    """
    cwd = os.getcwd().lower()
    # Deriva los paths healthcare desde config (fail-open: tupla vacía = solo env var activa PHI).
    try:
        aris_root_str = str(ARIS_ROOT)
        if aris_root_str not in sys.path:
            sys.path.insert(0, aris_root_str)
        from engine.v16.config import cfg_healthcare_clients  # noqa: PLC0415
        _hc_paths: tuple[str, ...] = tuple(f"03-clients/{c}" for c in cfg_healthcare_clients())
    except Exception:
        _hc_paths = ()
    on = (settings.get("env") or {}).get("ARIS4U_HEALTHCARE") == "1" or (
        bool(_hc_paths) and any(m in cwd for m in _hc_paths)
    )
    if on:
        return "🏥 PHI: ON"
    return "🏥 PHI: OFF · médico: aris-config --healthcare on"


def _build_briefing_inner() -> str:
    """Implementación real — separada para que build_briefing pueda atrapar todo."""
    ver = _plugin_version()
    settings = _load_settings()
    n_pretool, n_blocking = _count_pretool_hooks(settings)
    mem = _db_memory()
    n_vectors = _vector_count()
    last_digest = _last_digest_ts(mem)

    identity = IDENTITY_TMPL.format(owner=_owner_safe(), ver=ver)

    # Sección (ii): auto-automático
    n_total_hooks = sum(
        len(h.get("hooks", []))
        for hooks_list in settings.get("hooks", {}).values()
        for h in hooks_list
    )
    auto_block = (
        f"AUTO (no invocar): recall+hint cada prompt · "
        f"{n_total_hooks} hooks ({n_pretool} PreToolUse, {n_blocking} bloq) · "
        "captura commits SessionEnd"
    )

    phi_block = _phi_status(settings)  # siempre visible: OFF por defecto

    # Sección (iii): opt-in
    optin_block = (
        f"OPT-IN: MCP: {MCP_TOOLS}\n"
        "  Skills: /aris-council · /aris-status · /status <proyecto>\n"
        f"  Cuándo: recall_client(cliente) ANTES de {_clients_safe()};"
        " aris_search; aris_ingest; aris_dialectic"
    )

    # Sección (iv): memoria viva
    if mem.get("ok"):
        by_client_full = mem.get("by_client", [])
        clients_str = " · ".join(f"{c}:{n}" for c, n in by_client_full[:6])
        mem_block = (
            f"MEMORIA: {mem['decisions']} decisions · {mem['guards']} guards · "
            f"{mem['digests']} digests · {n_vectors} vectores\n"
            f"  por cliente: {clients_str}\n"
            f"  último digest: {last_digest}"
        )
        # Write-path alert
        age_h = mem.get("last_decision_age_h")
        if age_h is not None and age_h > 48:
            mem_block += f"\n  ⚠️ WRITE-PATH STALE: última decisión hace {age_h:.0f}h (>48h)"
    else:
        err = mem.get("error", "")
        mem_block = f"MEMORIA: sessions.db no legible{' — ' + err if err else ''}"

    # Sección (v): hardware (vía config o literal canónico si config no disponible)
    hw_block = f"HARDWARE:\n{_hardware_block_safe()}"

    # Sección (vi): postura de orquestación (Fase 3) — "" si no hay inventario vivo.
    posture_block = _orchestration_posture_safe()

    # Ensamblar bloque completo
    lines = [
        "── ARIS4U BRIEFING ──────────────────────────────────────",
        f"ID: {identity}",
        "",
        auto_block,
        phi_block,
        "",
        optin_block,
        "",
        mem_block,
        "",
        hw_block,
    ]
    if posture_block:
        lines += ["", posture_block]
    lines.append("─────────────────────────────────────────────────────────")
    full = "\n".join(lines)

    # Presupuesto duro: degrada si supera BUDGET_CHARS
    if len(full) > BUDGET_CHARS:
        # Degradar: by_client solo top-3, sin hw_block
        if mem.get("ok"):
            by_client_deg = mem.get("by_client", [])
            clients_short = " · ".join(f"{c}:{n}" for c, n in by_client_deg[:3])
            mem_block_short = (
                f"MEMORIA: {mem['decisions']} dec · {mem['guards']} guards · "
                f"{mem['digests']} dig · {n_vectors} vec\n"
                f"  top-3: {clients_short}"
            )
            age_h = mem.get("last_decision_age_h")
            if age_h is not None and age_h > 48:
                mem_block_short += f"  ⚠️ STALE {age_h:.0f}h"
        else:
            mem_block_short = mem_block
        lines_deg = [
            "── ARIS4U BRIEFING ─────────────────────────────────",
            f"ID: {identity}",
            "",
            auto_block,
            "",
            optin_block,
            "",
            mem_block_short,
        ]
        if posture_block:
            lines_deg += ["", posture_block]
        lines_deg.append("────────────────────────────────────────────────────")
        full = "\n".join(lines_deg)

    return full
