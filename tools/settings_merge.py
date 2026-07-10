#!/usr/bin/env python3
"""Merge idempotente del bloque env de ARIS4U en ~/.claude/settings.json.

SOLO añade las claves que FALTAN.  Nunca sobreescribe valores que el usuario ya tiene.
Hace backup (settings.json.bak) antes de cualquier escritura.  Idempotente: segunda
corrida con el mismo settings produce "Nothing to merge — already up to date."

Uso:
    # Ver qué añadiría (dry-run por defecto, sin tocar nada):
    python3 tools/settings_merge.py

    # Aplicar cambios (escribe backup + settings.json actualizado):
    python3 tools/settings_merge.py --apply

    # Ruta alternativa (e.g. un settings temporal de prueba):
    python3 tools/settings_merge.py --settings /tmp/test_settings.json --apply

Invocado también por el skill aris-onboard (Paso 4) para automatizar el único paso
genuinamente manual del onboarding.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Bloque env canónico de ARIS4U
# ──────────────────────────────────────────────────────────────────────────────

# Solo se AÑADEN claves que no existen en el settings.json actual.
# Claves ya presentes con cualquier valor se preservan intactas.
ARIS4U_ENV_BLOCK: dict[str, str] = {
    # Rendimiento y caché
    "ENABLE_PROMPT_CACHING_1H": "true",
    "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "15000",
    # ARIS4U core
    "ARIS4U_DEPTH_PROTOCOL": "1",
    "ARIS4U_CONDUCTOR_ENFORCE": "1",
    "ARIS4U_HEALTHCARE": "0",
    # Router semántico y recall
    "ARIS4U_ROUTER_SEM_THRESHOLD": "0.70",
    "ARIS4U_DIVERSE_RECALL": "0",
}

DEFAULT_SETTINGS_PATH: Path = Path.home() / ".claude" / "settings.json"


# ──────────────────────────────────────────────────────────────────────────────
# I/O
# ──────────────────────────────────────────────────────────────────────────────


def load_settings(path: Path) -> dict[str, Any]:
    """Carga settings.json desde disco.

    Args:
        path: Ruta al archivo settings.json.

    Returns:
        Dict parseado.

    Raises:
        SystemExit: Si el archivo no existe o no es JSON válido.
    """
    if not path.exists():
        print(f"[error] settings.json no encontrado: {path}", file=sys.stderr)
        raise SystemExit(2)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[error] JSON inválido en {path}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def write_settings(path: Path, data: dict[str, Any]) -> None:
    """Escribe settings.json a disco preservando el formato (indent=2).

    Args:
        path: Ruta destino.
        data: Dict de settings a serializar.
    """
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ──────────────────────────────────────────────────────────────────────────────
# Lógica de merge
# ──────────────────────────────────────────────────────────────────────────────

# Alias para reducir repetición en firmas y mantenerlas en una sola línea.
_StrEnv = dict[str, str]


def compute_additions(current_env: _StrEnv, desired_env: _StrEnv) -> _StrEnv:
    """Devuelve solo las claves de desired_env que NO están en current_env.

    Claves ya presentes (con cualquier valor) son preservadas — no se tocan.

    Args:
        current_env: Bloque env actual del settings.json del usuario.
        desired_env: Bloque env canónico a fusionar.

    Returns:
        Dict con solo las entradas que faltan en current_env.
    """
    return {k: v for k, v in desired_env.items() if k not in current_env}


def backup_settings(path: Path) -> Path:
    """Crea una copia de seguridad de settings.json en settings.json.bak.

    Sobrescribe el .bak existente (solo guarda el último backup).

    Args:
        path: Ruta del settings.json a respaldar.

    Returns:
        Ruta del archivo de backup creado.
    """
    bak = path.with_suffix(".json.bak")
    shutil.copy2(path, bak)
    return bak


def merge_env(settings: dict[str, Any], additions: _StrEnv) -> dict[str, Any]:
    """Devuelve un nuevo dict de settings con las adiciones aplicadas.

    No muta el dict original.

    Args:
        settings: Dict de settings.json completo.
        additions: Claves/valores a añadir en settings["env"].

    Returns:
        Nuevo dict de settings con env actualizado.
    """
    result = dict(settings)
    current_env = dict(result.get("env", {}))
    current_env.update(additions)
    result["env"] = current_env
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Punto de entrada principal (lógica de merge + reporte)
# ──────────────────────────────────────────────────────────────────────────────


def run_merge(settings_path: Path, apply: bool) -> int:
    """Calcula y opcionalmente aplica el merge idempotente.

    Args:
        settings_path: Ruta al settings.json a actualizar.
        apply: Si True, escribe el merge en disco (con backup). Si False, dry-run.

    Returns:
        0 en éxito, 1 si hubo error en la escritura.
    """
    settings = load_settings(settings_path)
    current_env: dict[str, str] = settings.get("env", {})
    additions = compute_additions(current_env, ARIS4U_ENV_BLOCK)

    if not additions:
        print("Nothing to merge — already up to date.")
        _print_present(current_env)
        return 0

    _print_plan(additions, current_env, apply=apply)

    if not apply:
        print("\n[dry-run] Sin cambios en disco. Pasa --apply para aplicar.")
        return 0

    # Backup + escritura atómica
    bak = backup_settings(settings_path)
    print(f"\n[backup] {bak}")

    updated = merge_env(settings, additions)
    try:
        write_settings(settings_path, updated)
    except OSError as exc:
        print(f"[error] No se pudo escribir {settings_path}: {exc}", file=sys.stderr)
        return 1

    print(f"[ok] {settings_path} actualizado ({len(additions)} claves añadidas).")
    print("[!] Reinicia la sesión de Claude Code para que las variables surtan efecto.")
    return 0


def _print_plan(additions: _StrEnv, current_env: _StrEnv, apply: bool) -> None:
    """Imprime el plan de merge en formato legible."""
    mode = "Aplicando" if apply else "Dry-run"
    print(f"\n{mode} — {len(additions)} claves nuevas a añadir:\n")
    for k, v in sorted(additions.items()):
        print(f"  + {k} = {v!r}")

    present = {k: current_env[k] for k in ARIS4U_ENV_BLOCK if k in current_env}
    if present:
        print(f"\n{len(present)} claves ya presentes (preservadas sin cambio):\n")
        for k, v in sorted(present.items()):
            print(f"  = {k} = {v!r}")


def _print_present(current_env: dict[str, str]) -> None:
    """Imprime las claves ARIS4U ya presentes en el settings."""
    present = {k: current_env[k] for k in ARIS4U_ENV_BLOCK if k in current_env}
    if present:
        print(f"\n{len(present)} claves ARIS4U ya configuradas:\n")
        for k, v in sorted(present.items()):
            print(f"  = {k} = {v!r}")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parsea los argumentos de línea de comandos.

    Args:
        argv: Lista de strings con los argumentos (sin el nombre del script).

    Returns:
        Namespace con apply y settings_path.
    """
    parser = argparse.ArgumentParser(
        prog="settings-merge",
        description=(
            "Fusiona el bloque env de ARIS4U en ~/.claude/settings.json de forma "
            "idempotente.  Por defecto es dry-run (solo muestra qué añadiría).  "
            "Pasa --apply para escribir en disco (hace backup .bak antes)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Escribe el merge en disco (backup automático en settings.json.bak).",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        metavar="PATH",
        help=f"Ruta a settings.json (default: {DEFAULT_SETTINGS_PATH}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Punto de entrada del merge de settings.

    Args:
        argv: Lista de strings con los argumentos CLI (sin el nombre del script).

    Returns:
        0 si tuvo éxito, distinto de 0 si hubo un error.
    """
    args = parse_args(argv)
    return run_merge(settings_path=args.settings, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
