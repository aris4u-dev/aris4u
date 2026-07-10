#!/usr/bin/env python3
"""Visor de configuración de ARIS4U (Capa 0 del wrapper).

Muestra TODA la configuración relevante de ARIS4U y dónde vive, para no tener que
abrir JSON a mano. Es principalmente lectura; la única mutación es `--set-model`,
que edita ~/.claude/settings.json con backup previo.

Uso:
    python3 tools/aris_config.py                  # tabla de config
    python3 tools/aris_config.py --json
    python3 tools/aris_config.py --set-model claude-opus-4-8   # fija el modelo por defecto
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

ARIS_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = Path.home() / ".claude" / "settings.json"
REPO_MCP = ARIS_ROOT / ".mcp.json"

# Llaves de configuración que un panel debería poder ver/editar.
ENV_KNOBS = (
    "ARIS4U_HEALTHCARE",      # opt-in del vertical PHI (phi_guard/phi_sanitizer)
    "ARIS4U_AUTOUPDATE",      # auto-adaptación: shadow/PR/auto
    "ARIS4U_VALIDATION_LOG",  # logging de validación
    "ARIS4U_LOG_FILE",        # sink de telemetría JSONL
    "ENABLE_PROMPT_CACHING_1H",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
    "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY",
)


def load_json(path: Path) -> dict[str, Any]:
    """Carga un JSON; {} si no existe o está roto."""
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def collect() -> dict[str, Any]:
    """Reúne la configuración efectiva de ARIS4U."""
    settings = load_json(SETTINGS)
    repo_mcp = load_json(REPO_MCP)
    env = settings.get("env") or {}
    global_mcp = list((settings.get("mcpServers") or {}).keys())
    repo_mcp_servers = list((repo_mcp.get("mcpServers") or {}).keys())
    dup = sorted(set(global_mcp) & set(repo_mcp_servers))
    return {
        "model_default": settings.get("model"),
        "env": {k: env.get(k, "(no fijado)") for k in ENV_KNOBS},
        "mcp_global": global_mcp,
        "mcp_repo": repo_mcp_servers,
        "mcp_duplicated": dup,
        "settings_path": str(SETTINGS),
    }


def set_model(model: str) -> str:
    """Fija settings.model con backup. Devuelve mensaje de resultado."""
    settings = load_json(SETTINGS)
    if not settings:
        return f"ERROR: no pude leer {SETTINGS}"
    backup = SETTINGS.with_suffix(".json.bak-set-model")
    shutil.copy2(SETTINGS, backup)
    prev = settings.get("model", "(ninguno)")
    settings["model"] = model
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    return f"modelo por defecto: {prev} → {model}  (backup: {backup.name})"


def set_healthcare(on: bool) -> str:
    """Enciende/apaga el modo PHI (switch ARIS4U_HEALTHCARE en settings.json env).

    Es la herramienta para activar la gobernanza PHI al trabajar con un cliente médico.
    OFF por defecto: sin esto, phi_guard/phi_sanitizer/mcp_guard-PHI solo actúan DENTRO
    de un proyecto cliente healthcare (red de seguridad). Toma efecto en la próxima sesión.
    """
    settings = load_json(SETTINGS)
    if not settings:
        return f"ERROR: no pude leer {SETTINGS}"
    backup = SETTINGS.with_suffix(".json.bak-set-healthcare")
    shutil.copy2(SETTINGS, backup)
    env = settings.setdefault("env", {})
    if on:
        env["ARIS4U_HEALTHCARE"] = "1"
        msg = "🏥 PHI mode: ON — phi_guard/phi_sanitizer/mcp_guard-PHI activos en todas las sesiones"
    else:
        env.pop("ARIS4U_HEALTHCARE", None)
        msg = "🏥 PHI mode: OFF (default) — sin gobernanza PHI salvo dentro de un proyecto cliente médico"
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    return f"{msg}  (efectivo en sesión nueva · backup: {backup.name})"


def render(data: dict[str, Any]) -> str:
    """Tabla legible de la configuración."""
    L: list[str] = ["ARIS4U — CONFIGURACIÓN EFECTIVA", ""]
    md = data["model_default"] or "(no fijado → arranca en el default de Claude Code)"
    L.append(f"  MODELO por defecto : {md}")
    L.append(f"  settings.json      : {data['settings_path']}")
    L.append("")
    L.append("  ENV / FLAGS:")
    for k, v in data["env"].items():
        L.append(f"      {k:<38} = {v}")
    L.append("")
    L.append(f"  MCP (global)  : {', '.join(data['mcp_global']) or '—'}")
    L.append(f"  MCP (repo)    : {', '.join(data['mcp_repo']) or '—'}")
    if data["mcp_duplicated"]:
        L.append(f"  ⚠ DUPLICADOS  : {', '.join(data['mcp_duplicated'])} "
                 "(definidos en global Y en .mcp.json del repo)")
    L.append("")
    phi_on = data["env"].get("ARIS4U_HEALTHCARE") == "1"
    L.append(f"  🏥 PHI mode    : {'ON (ARIS4U_HEALTHCARE=1)' if phi_on else 'OFF (default)'}")
    L.append("")
    L.append("  Cambiar modelo:  python3 tools/aris_config.py --set-model claude-opus-4-8")
    L.append("  PHI on/off    :  python3 tools/aris_config.py --healthcare on|off")
    return "\n".join(L)


def main(argv: list[str]) -> int:
    if "--set-model" in argv:
        i = argv.index("--set-model")
        if i + 1 >= len(argv):
            print("ERROR: --set-model requiere un id de modelo")
            return 2
        print(set_model(argv[i + 1]))
        return 0
    if "--healthcare" in argv:
        i = argv.index("--healthcare")
        val = argv[i + 1].lower() if i + 1 < len(argv) else ""
        if val not in ("on", "off"):
            print("ERROR: --healthcare requiere 'on' u 'off'")
            return 2
        print(set_healthcare(val == "on"))
        return 0
    data = collect()
    if "--json" in argv:
        print(json.dumps(data, indent=2, default=str))
        return 0
    print(render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
