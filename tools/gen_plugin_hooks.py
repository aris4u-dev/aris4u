#!/usr/bin/env python3
"""Genera hooks/hooks.json (manifiesto de hooks del plugin) a partir de los hooks de
ARIS4U en ~/.claude/settings.json, reescribiendo los paths absolutos a variables
portables del plugin (${CLAUDE_PLUGIN_ROOT}).

Fuentes combinadas:
  1. hooks en settings.json que referencian el repo ARIS4U (dispatcher central) →
     path absoluto del repo reemplazado por ${CLAUDE_PLUGIN_ROOT}.
  2. hooks en settings.json que referencian ~/.claude/hooks/<nombre> Y tienen un
     archivo equivalente en hooks/standalone/<nombre> → path reescrito a
     ${CLAUDE_PLUGIN_ROOT}/hooks/standalone/<nombre>; intérprete Python personal
     reemplazado por ${CLAUDE_PLUGIN_ROOT}/.venv312/bin/python3.

Este segundo paso es el que corrige el hallazgo P2-M15: el generador anterior
filtraba solo 'projects/aris4u' y perdía todos los hooks standalone.

NO toca settings.json (solo lee). Re-ejecutar cuando cambien los hooks.
Uso: python tools/gen_plugin_hooks.py
"""
import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("ARIS4U_ROOT") or Path(__file__).resolve().parents[1])
SETTINGS = Path.home() / ".claude" / "settings.json"
OUT = ROOT / "hooks" / "hooks.json"
STANDALONE_DIR = ROOT / "hooks" / "standalone"
ABS_PREFIX = str(ROOT)  # p.ej. /Users/xxx/projects/aris4u

# Patrón del intérprete Python personal que el usuario usa para los standalone hooks.
# Se reemplaza por el venv del plugin. El pattern es amplio: cualquier python3 absoluto.
_PY_PATTERN = re.compile(r"/[^\s]*/(?:bin/|)python3?(?=\s)")


def _plugin_py() -> str:
    return "${CLAUDE_PLUGIN_ROOT}/.venv312/bin/python3"


def rewrite(cmd: str) -> str:
    """Path absoluto del repo ARIS4U -> variable del plugin."""
    return cmd.replace(ABS_PREFIX, "${CLAUDE_PLUGIN_ROOT}")


# Alias kept for internal callers that are explicit about what they rewrite.
rewrite_aris = rewrite


def rewrite_standalone(cmd: str, name: str) -> str:
    """Reescribe un hook personal (~/.claude/hooks/<name>) a su equivalente standalone.

    Sustituye:
      - El path completo del script personal por ${CLAUDE_PLUGIN_ROOT}/hooks/standalone/<name>
      - El intérprete Python absoluto personal por ${CLAUDE_PLUGIN_ROOT}/.venv312/bin/python3
    """
    home_hook = str(Path.home() / ".claude" / "hooks" / name)
    plugin_hook = f"${{CLAUDE_PLUGIN_ROOT}}/hooks/standalone/{name}"

    # Para scripts .sh: `bash "/path/to/hook.sh"` → `bash ${CLAUDE_PLUGIN_ROOT}/...`
    cmd = cmd.replace(f'bash "{home_hook}"', f"bash {plugin_hook}")
    cmd = cmd.replace(f"bash {home_hook}", f"bash {plugin_hook}")

    # Para scripts .py: reemplaza el script personal
    cmd = cmd.replace(home_hook, plugin_hook)

    # Reemplaza el intérprete Python personal por el del venv del plugin
    cmd = _PY_PATTERN.sub(_plugin_py() + " ", cmd, count=1).strip()

    return cmd


def is_aris_hook(cmd: str) -> bool:
    return "projects/aris4u" in cmd or ABS_PREFIX in cmd


def is_personal_hook(cmd: str) -> str | None:
    """Si el comando apunta a ~/.claude/hooks/<nombre> y hay equivalente standalone,
    devuelve el nombre del archivo; si no, None."""
    home_hooks_dir = str(Path.home() / ".claude" / "hooks" / "")
    if home_hooks_dir not in cmd and f'"{str(Path.home())}/.claude/hooks/' not in cmd:
        return None
    # Extrae el nombre del archivo del path
    m = re.search(r'\.claude/hooks/([^"\s]+)', cmd)
    if not m:
        return None
    name = Path(m.group(1)).name
    if (STANDALONE_DIR / name).is_file():
        return name
    return None


def _portable_hook(h: dict, rewrite_fn, cmd_arg: str) -> dict:
    """Devuelve un hook con command reescrito y timeout preservado."""
    entry: dict = {"type": "command", "command": rewrite_fn(cmd_arg)}
    if "timeout" in h:
        entry["timeout"] = h["timeout"]
    return entry


def _collect_hooks(raw_hooks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separa los hooks de un grupo en (aris, standalone)."""
    aris: list[dict] = []
    standalone: list[dict] = []
    for h in raw_hooks:
        cmd = h.get("command", "")
        if is_aris_hook(cmd):
            aris.append(_portable_hook(h, rewrite_aris, cmd))
        else:
            name = is_personal_hook(cmd)
            if name:
                standalone.append(_portable_hook(h, lambda c, n=name: rewrite_standalone(c, n), cmd))
    return aris, standalone


def _process_group(g: dict) -> list[dict]:
    """Convierte un grupo de settings.json en 0-2 grupos portables."""
    matcher = g.get("matcher")
    aris, standalone = _collect_hooks(g.get("hooks", []))
    result = []
    for hook_list in (aris, standalone):
        if not hook_list:
            continue
        ng: dict = {}
        if matcher:
            ng["matcher"] = matcher
        ng["hooks"] = hook_list
        result.append(ng)
    return result


def _build_out_hooks(settings: dict) -> dict:
    out: dict = {}
    for event, groups in settings.get("hooks", {}).items():
        new_groups = [ng for g in groups for ng in _process_group(g)]
        if new_groups:
            out[event] = new_groups
    return out


def _sanity_report(out_hooks: dict) -> int:
    OUT.write_text(json.dumps({"hooks": out_hooks}, indent=2) + "\n")
    n = sum(len(g["hooks"]) for gs in out_hooks.values() for g in gs)
    n_standalone = sum(
        1
        for gs in out_hooks.values()
        for g in gs
        for h in g["hooks"]
        if "standalone" in h.get("command", "")
    )
    text = OUT.read_text()
    leftover_abs = text.count(ABS_PREFIX)
    leftover_home = text.count(str(Path.home() / ".claude" / "hooks"))
    print(f"hooks.json: {n} hooks en {len(out_hooks)} eventos -> {OUT}")
    print(f"  standalone hooks incluidos: {n_standalone}")
    print(f"  paths absolutos del repo restantes: {leftover_abs} (debe ser 0)")
    print(f"  paths ~/.claude/hooks personales restantes: {leftover_home} (debe ser 0)")
    return 0 if (leftover_abs == 0 and leftover_home == 0) else 1


def main() -> int:
    settings = json.loads(SETTINGS.read_text())
    out_hooks = _build_out_hooks(settings)
    return _sanity_report(out_hooks)


if __name__ == "__main__":
    raise SystemExit(main())
