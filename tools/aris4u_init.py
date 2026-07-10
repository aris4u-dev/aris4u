#!/usr/bin/env python3
"""Comando aris4u init: genera la config por-usuario que hace ARIS4U instalable por terceros.

Detecta hardware portable, escanea proyectos con .git, y escribe ~/.aris4u/config.json
de forma IDEMPOTENTE: fusiona con config existente preservando claves del usuario
(sobre todo ollama_*/w2_*). Nunca sobrescribe a ciegas.

Uso:
    python3 tools/aris4u_init.py [--yes] [--dry-run] [--force] [--scan-root ~/projects]
    python3 -m tools.aris4u_init [opciones]

Flags:
    --yes           Modo no interactivo: incluir todos los proyectos detectados.
    --dry-run       Imprime el JSON resultante sin escribir en disco.
    --force         Re-inicializa ignorando la config existente.
    --scan-root     Directorio raíz de proyectos (default: ~/projects).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────────────────────────────────────

ARIS_DIR: Path = Path.home() / ".aris4u"

# Claves auto-detectadas siempre recomputadas (no se preservan del config previo).
ALWAYS_RECOMPUTE: frozenset[str] = frozenset({"hardware", "lab_projects"})

# Workers y nodos muertos: VACÍO por defecto para instalaciones de terceros.
# Cada usuario configura los suyos en ~/.aris4u/config.json (sección hardware.workers
# y hardware.dead). El init los preserva en re-ejecuciones; --force arranca limpio.
# Ejemplo de entrada en workers:
#   {"name": "w2", "ssh": "w2", "gpu": "RTX 3070 Laptop (8 GB)", "enabled": true,
#    "note": "AMD Ryzen 9 5900HX · Pop!_OS · verificar RAM libre antes de despachar"}
KNOWN_WORKERS: list[dict[str, Any]] = []
KNOWN_DEAD: list[str] = []

# Palabras clave para inferir si un cliente es healthcare.
HEALTHCARE_KEYWORDS: frozenset[str] = frozenset(
    {"health", "medical", "hospital", "radiology", "phi"}
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers de subproceso
# ──────────────────────────────────────────────────────────────────────────────


def _run_cmd(cmd: list[str], timeout: int = 2) -> str:
    """Ejecuta un comando y devuelve stdout normalizado. '' si falla.

    Args:
        cmd: Lista de strings para subprocess.run.
        timeout: Timeout en segundos.

    Returns:
        stdout como string; vacío si el comando falla o excede el timeout.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.stdout.strip()
    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────────────────────
# Detección de RAM (best-effort, never raises)
# ──────────────────────────────────────────────────────────────────────────────


def _detect_ram_gb() -> float | None:
    """Detecta RAM total en GB. psutil si disponible; fallback sysctl/proc.

    Returns:
        RAM en GB con 1 decimal, o None si no pudo detectarse.
    """
    try:
        import psutil  # type: ignore[import]

        return round(psutil.virtual_memory().total / (1024**3), 1)
    except Exception:
        pass

    system = platform.system()
    if system == "Darwin":
        out = _run_cmd(["sysctl", "-n", "hw.memsize"])
        if out:
            try:
                return round(int(out) / (1024**3), 1)
            except Exception:
                pass
    elif system == "Linux":
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024**2), 1)
        except Exception:
            pass

    return None


# ──────────────────────────────────────────────────────────────────────────────
# Detección de chip — Darwin y Linux en funciones separadas (CC reducida)
# ──────────────────────────────────────────────────────────────────────────────


def _detect_chip_darwin() -> str | None:
    """Detecta el nombre del chip en macOS (Intel o Apple Silicon).

    Returns:
        String del chip, o None si no pudo detectarse.
    """
    # Intel: machdep.cpu.brand_string existe y devuelve algo útil
    out = _run_cmd(["sysctl", "-n", "machdep.cpu.brand_string"])
    if out:
        return out

    # Apple Silicon: system_profiler (más lento; best-effort)
    sp_out = _run_cmd(["system_profiler", "SPHardwareDataType"], timeout=5)
    for line in sp_out.splitlines():
        if "Chip" in line or "Processor Name" in line:
            parts = line.split(":", 1)
            if len(parts) == 2 and parts[1].strip():
                return parts[1].strip()

    # Fallback: al menos sabemos que es ARM
    if platform.machine() == "arm64":
        return "Apple Silicon (arm64)"

    return None


def _detect_chip_linux() -> str | None:
    """Detecta el nombre del chip en Linux via lscpu.

    Returns:
        String del chip, o None si lscpu no está disponible o no da el campo.
    """
    out = _run_cmd(["lscpu"])
    for line in out.splitlines():
        if "Model name" in line:
            parts = line.split(":", 1)
            if len(parts) == 2:
                return parts[1].strip()
    return None


def _detect_chip() -> str | None:
    """Detecta nombre del chip (best-effort, Darwin y Linux).

    Returns:
        String descriptivo del chip, o None si no pudo detectarse.
    """
    system = platform.system()
    if system == "Darwin":
        return _detect_chip_darwin()
    if system == "Linux":
        return _detect_chip_linux()
    return None


def _detect_gpu() -> str | None:
    """Detecta GPU via nvidia-smi (opcional, timeout estricto de 3 s).

    Returns:
        Descripción de la GPU, o None si nvidia-smi no está disponible.
    """
    out = _run_cmd(
        ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
        timeout=3,
    )
    if out:
        return out.splitlines()[0].strip()
    return None


# ──────────────────────────────────────────────────────────────────────────────
# detect_hardware — never raises
# ──────────────────────────────────────────────────────────────────────────────


def detect_hardware(
    workers: list[dict[str, Any]] | None = None,
    dead: list[str] | None = None,
) -> dict[str, Any]:
    """Auto-detecta hardware del sistema. Nunca lanza excepción.

    Workers y dead se pasan explícitamente en vez de leerse de constantes de módulo
    para que un tercero arranque con listas vacías y el usuario preserve su cluster
    (w2, etc.) leyendo la config existente antes de llamar a esta función.

    Args:
        workers: Lista de workers remotos del usuario. None usa KNOWN_WORKERS (vacío
            por defecto; un instalador de tercero parte sin workers hardcodeados).
        dead: Lista de nombres de nodos inactivos. None usa KNOWN_DEAD (vacío por
            defecto).

    Returns:
        Dict con auto_detect, primary (label/cores/arch/platform, ram_gb si detectado,
        gpu si detectado), workers y dead.
    """
    if workers is None:
        workers = list(KNOWN_WORKERS)
    if dead is None:
        dead = list(KNOWN_DEAD)

    cores: int = os.cpu_count() or 0
    arch: str = platform.machine()
    system: str = platform.system()
    ram_gb: float | None = _detect_ram_gb()
    chip: str | None = _detect_chip()
    gpu: str | None = _detect_gpu()

    primary: dict[str, Any] = {
        "label": chip or f"{arch} ({system})",
        "cores": cores,
        "arch": arch,
        "platform": system,
    }
    if ram_gb is not None:
        primary["ram_gb"] = ram_gb
    if gpu:
        primary["gpu"] = gpu

    return {
        "auto_detect": True,
        "primary": primary,
        "workers": workers,
        "dead": dead,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Helpers para escaneo de proyectos (CC extraída de scan_projects)
# ──────────────────────────────────────────────────────────────────────────────


def _has_git(path: Path) -> bool:
    """True si el directorio contiene un repo .git."""
    return (path / ".git").exists()


def _has_aris_client(path: Path) -> bool:
    """True si el directorio tiene marcador .aris-client."""
    return (path / ".aris-client").exists()


def _collect_repo_paths(scan_root: Path) -> list[Path]:
    """Colecta todos los dirs con .git bajo scan_root y scan_root/03-clients/.

    Deduplicar por ruta resuelta para evitar entradas dobles por symlinks.

    Args:
        scan_root: Directorio raíz de proyectos a escanear.

    Returns:
        Lista de paths únicos (resueltos) que contienen un repo .git.
    """
    candidates: list[Path] = []
    for root in [scan_root, scan_root / "03-clients"]:
        if not root.exists():
            continue
        try:
            for item in sorted(root.iterdir()):
                if item.is_dir() and _has_git(item):
                    candidates.append(item)
        except Exception:
            continue

    seen: set[Path] = set()
    unique: list[Path] = []
    for c in candidates:
        try:
            resolved = c.resolve()
        except Exception:
            resolved = c
        if resolved not in seen:
            seen.add(resolved)
            unique.append(c)
    return unique


def _infer_topic(path: Path) -> str:
    """Infiere el topic de un proyecto: 'client', 'aris-client', o 'lab'.

    Args:
        path: Ruta del directorio del proyecto.

    Returns:
        String de topic: 'client' si está bajo 03-clients/,
        'aris-client' si tiene marcador, 'lab' en caso contrario.
    """
    if "03-clients" in str(path):
        return "client"
    if _has_aris_client(path):
        return "aris-client"
    return "lab"


def _display_path(path: Path) -> str:
    """Formatea la ruta como ~/... sustituyendo el home dir.

    Args:
        path: Ruta absoluta del directorio.

    Returns:
        Ruta con ~ en vez del directorio home.
    """
    path_str = str(path)
    home_str = str(Path.home())
    if path_str.startswith(home_str):
        return "~" + path_str[len(home_str):]
    return path_str


def _confirm_include(path: Path, topic: str, yes: bool) -> bool:
    """Retorna True si el proyecto debe incluirse en la config.

    En modo --yes, incluye todo. En modo interactivo, pregunta al usuario.

    Args:
        path: Ruta del proyecto a confirmar.
        topic: Topic inferido del proyecto.
        yes: Si True, incluye sin preguntar.

    Returns:
        True si el proyecto debe incluirse.
    """
    if yes:
        return True
    try:
        ans = input(f"  Include project '{path.name}' ({topic})? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("", "y", "yes")


def scan_projects(scan_root: Path, yes: bool = False) -> list[dict[str, Any]]:
    """Escanea scan_root y scan_root/03-clients/ buscando repos git.

    Args:
        scan_root: Directorio raíz de proyectos.
        yes: Si True, incluye todos sin confirmación interactiva.

    Returns:
        Lista de dicts {dir, db_project, topic, path} de labs candidatos.
    """
    unique = _collect_repo_paths(scan_root)
    labs: list[dict[str, Any]] = []
    for path in unique:
        topic = _infer_topic(path)
        if not _confirm_include(path, topic, yes):
            continue
        labs.append({
            "dir": path.name,
            "db_project": path.name,
            "topic": topic,
            "path": _display_path(path),
        })
    return labs


def _detect_clients(scan_root: Path) -> tuple[list[str], list[str]]:
    """Detecta clientes de scan_root/03-clients/ y separa los healthcare.

    Args:
        scan_root: Directorio raíz de proyectos.

    Returns:
        Tupla (clients, healthcare_clients), ambas listas de strings.
    """
    clients: list[str] = []
    healthcare_clients: list[str] = []

    clients_dir = scan_root / "03-clients"
    if not clients_dir.exists():
        return clients, healthcare_clients

    try:
        for item in sorted(clients_dir.iterdir()):
            if item.is_dir():
                name = item.name
                clients.append(name)
                if any(kw in name.lower() for kw in HEALTHCARE_KEYWORDS):
                    healthcare_clients.append(name)
    except Exception:
        pass

    return clients, healthcare_clients


# ──────────────────────────────────────────────────────────────────────────────
# Owner
# ──────────────────────────────────────────────────────────────────────────────


def _detect_owner() -> str:
    """Detecta el owner: git config user.name si disponible, si no $USER.

    Returns:
        Nombre del propietario como string.
    """
    out = _run_cmd(["git", "config", "user.name"])
    if out:
        return out
    return os.environ.get("USER", os.environ.get("USERNAME", "unknown"))


# ──────────────────────────────────────────────────────────────────────────────
# Resolución de ruta de config
# ──────────────────────────────────────────────────────────────────────────────


def _config_path() -> Path:
    """Resuelve la ruta efectiva de config respetando $ARIS4U_CONFIG.

    Returns:
        Path de config: $ARIS4U_CONFIG si definido, si no ~/.aris4u/config.json.
    """
    env_path = os.environ.get("ARIS4U_CONFIG", "").strip()
    if env_path:
        return Path(env_path)
    return ARIS_DIR / "config.json"


# ──────────────────────────────────────────────────────────────────────────────
# Carga de config existente
# ──────────────────────────────────────────────────────────────────────────────


def load_existing(config_path: Path) -> dict[str, Any]:
    """Carga config existente de disco. Devuelve {} si no existe o está rota.

    Args:
        config_path: Ruta del archivo JSON de config.

    Returns:
        Dict de config, o {} si el archivo no existe o es inválido.
    """
    try:
        return json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Construcción y fusión de config
# ──────────────────────────────────────────────────────────────────────────────

# Alias local para evitar que la firma de build_config supere los 100 chars.
_Cfg = dict[str, Any]


def build_config(scan_root: Path, yes: bool, force: bool, existing: _Cfg | None = None) -> _Cfg:
    """Construye la config completa, fusionando con la existente.

    Estrategia de fusión (sin --force):
    - hardware y lab_projects: siempre recomputados (ALWAYS_RECOMPUTE).
    - Todas las demás claves del config existente: PRESERVADAS
      (owner, ollama_*, w2_*, clients, healthcare_clients, custom keys).

    Con --force: se ignora el config existente y se recomputa todo.

    Args:
        scan_root: Directorio raíz para escaneo de proyectos.
        yes: Modo no interactivo (incluir todo sin preguntar).
        force: Re-inicializar ignorando existente.
        existing: Config previa cargada (None trata igual que {}).

    Returns:
        Config dict lista para serializar como JSON.
    """
    if existing is None:
        existing = {}

    # Preserve user-defined workers/dead from existing config so personal cluster
    # data is never hardcoded at module level.  --force resets to empty defaults,
    # which is the correct clean-install behavior for a third party.
    existing_hw: dict[str, Any] = {} if force else existing.get("hardware", {})
    hw_workers: list[dict[str, Any]] = existing_hw.get("workers", KNOWN_WORKERS)
    hw_dead: list[str] = existing_hw.get("dead", KNOWN_DEAD)
    hardware = detect_hardware(workers=hw_workers, dead=hw_dead)
    lab_projects = scan_projects(scan_root, yes=yes)
    owner = _detect_owner()
    clients, healthcare_clients = _detect_clients(scan_root)

    new_config: dict[str, Any] = {
        "_comment": (
            "Config generada por 'aris4u init'. "
            "NO versiones este archivo (.gitignore lo excluye). "
            "Re-corre: python3 tools/aris4u_init.py"
        ),
        "owner": owner,
        "hardware": hardware,
        "lab_projects": lab_projects,
        "clients": clients,
        "healthcare_clients": healthcare_clients,
        "ollama_mac_url": "http://localhost:11434",
        # ollama_w2_url / w2_ssh / w2_enabled: omitidos del default para terceros.
        # Si tienes un worker remoto, añádelos manualmente en ~/.aris4u/config.json
        # o configura hardware.workers arriba (aris4u init --force los restablece vacíos).
    }

    if force or not existing:
        return new_config

    # Fusión: nueva config como base; preservar claves de usuario excepto ALWAYS_RECOMPUTE
    merged: dict[str, Any] = {**new_config}
    for k, v in existing.items():
        if k not in ALWAYS_RECOMPUTE:
            merged[k] = v
    return merged


# ──────────────────────────────────────────────────────────────────────────────
# Escritura y validación
# ──────────────────────────────────────────────────────────────────────────────


def write_config(config: dict[str, Any], config_path: Path) -> None:
    """Escribe la config en disco con permisos 0o600.

    Crea el directorio padre si no existe (exist_ok).

    Args:
        config: Dict de configuración a serializar.
        config_path: Ruta destino.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    config_path.chmod(0o600)


def validate_config(config_path: Path) -> dict[str, Any]:
    """Re-lee y valida la config escrita en disco.

    Args:
        config_path: Ruta del archivo a validar.

    Returns:
        Dict parseado.

    Raises:
        ValueError: Si el archivo no se puede leer o es JSON inválido.
    """
    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Config invalida en {config_path}: {exc}") from exc
    return data


# ──────────────────────────────────────────────────────────────────────────────
# Resumen de salida
# ──────────────────────────────────────────────────────────────────────────────


def print_summary(config: dict[str, Any], config_path: Path | None = None) -> None:
    """Imprime un resumen human-readable de la config generada.

    Args:
        config: Config dict validado.
        config_path: Ruta donde se escribió. None indica modo dry-run.
    """
    hw = config.get("hardware", {})
    primary = hw.get("primary", {})
    labs = config.get("lab_projects", [])
    clients = config.get("clients", [])
    healthcare = config.get("healthcare_clients", [])

    ram_part = f" · {primary['ram_gb']} GB" if "ram_gb" in primary else ""
    hw_line = (
        f"  hardware     : {primary.get('label', '—')}  "
        f"({primary.get('cores', '?')} cores{ram_part} · {primary.get('arch', '?')})"
    )
    clients_line = (
        f"  clients      : {len(clients)}"
        + (f" ({', '.join(clients)})" if clients else "")
    )
    dest_line = (
        f"  config       : {config_path}"
        if config_path
        else "  modo         : --dry-run (NO se escribio en disco)"
    )

    lines = [
        "",
        "ARIS4U INIT — Resumen de configuracion",
        "─" * 45,
        f"  owner        : {config.get('owner', '—')}",
        hw_line,
        f"  lab_projects : {len(labs)} detectados",
        clients_line,
        f"  healthcare   : {', '.join(healthcare) if healthcare else '—'}",
        dest_line,
        "",
    ]
    print("\n".join(lines))


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parsea los argumentos de línea de comandos.

    Args:
        argv: Lista de strings con los argumentos (sin el nombre del script).

    Returns:
        Namespace con los atributos yes, dry_run, force, scan_root.
    """
    parser = argparse.ArgumentParser(
        prog="aris4u-init",
        description=(
            "Genera la config por-usuario de ARIS4U (~/.aris4u/config.json). "
            "Idempotente: fusiona con config existente preservando claves del usuario."
        ),
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Modo no interactivo: incluir todos los proyectos detectados sin preguntar.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Imprime el JSON resultante sin escribir en disco.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-inicializa ignorando la config existente (no fusiona).",
    )
    parser.add_argument(
        "--scan-root",
        type=Path,
        default=Path.home() / "projects",
        metavar="DIR",
        help="Directorio raíz de proyectos a escanear (default: ~/projects).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Punto de entrada del comando aris4u init.

    Args:
        argv: Lista de strings con los argumentos CLI (sin el nombre del script).

    Returns:
        0 si tuvo éxito, distinto de 0 si hubo un error.
    """
    args = parse_args(argv)
    config_path = _config_path()

    existing: dict[str, Any]
    if args.force:
        existing = {}
    else:
        existing = load_existing(config_path)
        if existing:
            print(
                f"Config existente en {config_path} — fusionando "
                "(use --force para reinicializar desde cero)"
            )

    config = build_config(
        scan_root=args.scan_root,
        yes=args.yes,
        force=args.force,
        existing=existing,
    )

    if args.dry_run:
        print(json.dumps(config, indent=2, ensure_ascii=False))
        print("---")
        print_summary(config, config_path=None)
        return 0

    write_config(config, config_path)

    try:
        validated = validate_config(config_path)
    except ValueError as exc:
        print(f"ERROR de validacion post-escritura: {exc}", file=sys.stderr)
        return 1

    print_summary(validated, config_path=config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
