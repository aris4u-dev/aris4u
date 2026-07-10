"""mcp_guard — telemetría de tools MCP + (solo en modo healthcare) deny de PHI→egress.

Cierra el gap de visibilidad de la auditoría 2026-06-22: ningún matcher incluía
``mcp__*`` → las llamadas MCP pasaban sin rastro. Este handler las REGISTRA.

Filosofía (alineada con PHI off-by-default): NO estorbar el trabajo legítimo. Por
defecto es SILENCIOSO — solo telemetría. NO trata herramientas financieras (contabilidad/pagos) como prohibidas: pueden ser trabajo legítimo. Un veto tipo "finanzas off
limits" sería para NO tocar una carpeta/módulo específico de un proyecto, no un veto a
las herramientas. La ÚNICA acción que bloquea es PHI saliendo a un servicio
externo, y SOLO cuando el modo healthcare está activo (opt-in; ver phi_guard).

Como función pura ``(tool_name, tool_input, cwd) -> Verdict``:
  - SIEMPRE: registra ``mcp_call`` (server/tool/familia) — invisible, sin fricción.
  - SOLO en modo healthcare: DENY si hay PHI en los parámetros hacia egress externo
    (Drive/atlassian/chrome/Figma). Override de sesión ``ARIS4U_MCP_ALLOW=1``.
  - Todo lo demás: PASS (silencioso). Fail-open total.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, UTC

from dispatch.contract import ARIS4U_ROOT
from dispatch.handlers import phi_guard as _phi
from dispatch.handlers import pre_common
from dispatch.handlers import verdict as V

# Tools de supabase que ESCRIBEN o cambian esquema/infra (para etiqueta de telemetría).
_SUPABASE_WRITE = {
    "execute_sql", "apply_migration", "deploy_edge_function", "create_branch",
    "delete_branch", "merge_branch", "rebase_branch", "reset_branch",
    "create_project", "pause_project", "restore_project",
}

# Tools que SACAN datos a un servicio externo (egress), por servidor.
_EGRESS = {
    "claude_ai_Google_Drive": {"create_file", "copy_file"},
    "claude-in-chrome": {"file_upload", "upload_image"},
    "claude_ai_Figma": {"upload_assets", "use_figma", "create_new_file", "send_code_connect_mappings"},
}
# atlassian: cualquier mutación (empuja datos a Jira/Confluence) = egress.
_ATLASSIAN_EGRESS_PREFIXES = ("create", "add", "update", "edit", "transition")

_OVERRIDE_ENV = "ARIS4U_MCP_ALLOW"


def _parse(tool_name: str) -> tuple[str, str] | None:
    """``mcp__<server>__<tool>`` → ``(server, tool)``; None si no es un tool MCP."""
    if not tool_name.startswith("mcp__"):
        return None
    rest = tool_name[len("mcp__"):]
    server, sep, tool = rest.partition("__")
    if not sep:
        return None
    return server, tool


def _is_egress(server: str, tool: str) -> bool:
    """True si la llamada saca datos a un servicio externo."""
    if server == "atlassian":
        return tool.startswith(_ATLASSIAN_EGRESS_PREFIXES)
    return tool in _EGRESS.get(server, set())


def _classify(server: str, tool: str) -> str:
    """Familia para la telemetría: datos/egress/lectura (sin caso 'financiero')."""
    if server == "supabase" and tool in _SUPABASE_WRITE:
        return "datos"
    if _is_egress(server, tool):
        return "egress"
    return "lectura"


def _log(server: str, tool: str, family: str) -> None:
    """Telemetría: deja rastro de TODA llamada MCP (antes invisibles). Fail-open."""
    log_file = ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"
    if not log_file.parent.is_dir():
        return
    try:
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        event = {"event": "mcp_call", "hook": "mcp_guard", "ts": ts,
                 "server": server, "tool": tool, "family": family}
        with open(log_file, "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


def _override_active() -> bool:
    """True si el usuario habilitó el override de MCP para esta sesión (explícito)."""
    return os.environ.get(_OVERRIDE_ENV, "").strip().lower() not in ("", "0", "false")


def _phi_egress_deny(server: str, tool: str, family: str, tool_input: dict, cwd: str) -> str:
    """Razón de deny por PHI→egress, o '' si no aplica.

    Solo bloquea si: (a) la llamada es egress externo, (b) hay un patrón PHI en los
    parámetros, y (c) el modo healthcare está activo (``_phi._is_healthcare_ctx`` =
    opt-in, off por defecto). Fuera de healthcare nunca dispara.
    """
    if family != "egress":
        return ""
    text = (pre_common.tool_text(tool_input) or "").lower()
    if not text or not _phi._is_healthcare_ctx(cwd or "", text):
        return ""
    phi = next((p for p in _phi._PHI_PATTERNS if re.search(p, text)), "")
    if not phi:
        return ""
    return (
        f"MCP PHI→EGRESS: {server} → {tool} lleva PHI en los parámetros hacia un "
        f"servicio externo (patrón: {phi})."
    )


def check(tool_name: str, tool_input: dict, cwd: str = "") -> V.Verdict:
    """Telemetría siempre; deny SOLO de PHI→egress en modo healthcare. Si no, PASS.

    Args:
        tool_name: nombre del tool (``mcp__<server>__<tool>`` para MCP).
        tool_input: parámetros de la llamada (se escanea PHI solo en modo healthcare).
        cwd: directorio de trabajo (para el gate healthcare).

    Returns:
        DENY si PHI→egress en modo healthcare (salvo override); si no, PASS.
    """
    parsed = _parse(tool_name)
    if parsed is None:
        return V.ok()
    server, tool = parsed
    family = _classify(server, tool)
    _log(server, tool, family)

    reason = _phi_egress_deny(server, tool, family, tool_input or {}, cwd)
    if reason:
        if _override_active():
            return V.advise(f"⚠️ MCP (override {_OVERRIDE_ENV} activo): {reason}")
        _log(server, tool, "deny")
        return V.deny(f"🛑 {reason} Override de sesión: export {_OVERRIDE_ENV}=1")

    return V.ok()
