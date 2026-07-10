#!/usr/bin/env python3
"""Gate de contrato de ARIS4U — pre-flight de seguridad antes de confiar en auto-adaptación.

El smoke histórico (`smoke_roundtrip.py`) solo probaba el roundtrip de memoria. El P0 de
operación (MASTER §5) pedía un gate que también ejercite la ruta del modelo local y verifique
que los guards SIGUEN bloqueando — sin eso, activar auto-adaptación es inseguro (un guard roto +
un lazo que cierra cambios = daño silencioso). Este gate compone TRES contratos vivos:

  1. MEMORIA   — roundtrip write→recall real (reusa ``smoke_roundtrip.run``).
  2. RUTA LOCAL — el cuerpo local responde (``model_dispatcher.health_check``). Advisory: el local
                 es lazy/opt-in, así que su ausencia AVISA pero no tumba el gate por sí sola.
  3. GUARDS    — los dos guards BLOQUEANTES (migration_linter, phi_guard) realmente bloquean,
                 verificado por sus tests autoritativos (no por un input frágil hecho a mano).

Exit 0 = los contratos CORE (memoria + guards) están vivos → seguro operar/auto-adaptar.
Exit 1 = algún contrato core roto → NO auto-adaptar.

Uso:  python3 tools/contract_gate.py [--skip-route]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone, UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "contract_gate.jsonl"
PYBIN = str(ROOT / ".venv312" / "bin" / "python")
# Tests autoritativos de que los guards bloqueantes BLOQUEAN (exit 2 del hook).
# Expresión -k de keywords puros (NO nodeids con ruta — eso rompe el selector).
_GUARD_TESTS = "test_migration_bad_blocks_exit2 or test_phi_healthcare_external_blocks_exit2"


def _log(event: dict) -> None:
    """Append fail-soft a logs/contract_gate.jsonl (consumible por el reporte de viernes)."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def check_memory() -> tuple[bool, str]:
    """Contrato 1: el roundtrip de memoria write→recall está vivo."""
    try:
        sys.path.insert(0, str(ROOT))
        from tools.smoke_roundtrip import run  # noqa: E402
        ok = run() == 0
        return ok, "roundtrip vivo" if ok else "roundtrip ROTO"
    except Exception as e:  # noqa: BLE001 — el gate nunca debe lanzar
        return False, f"excepción:{type(e).__name__}"


def check_local_route() -> tuple[bool, str]:
    """Contrato 2: el cuerpo local (Ollama Mac) responde. Advisory (lazy/opt-in)."""
    try:
        sys.path.insert(0, str(ROOT))
        from engine.v16 import model_dispatcher  # noqa: E402
        health = model_dispatcher.health_check()
        up = bool(health.get("mac", {}).get("ollama"))
        n = len(health.get("mac", {}).get("models", []))
        return up, f"Mac Ollama {'UP' if up else 'DOWN (lazy)'} · {n} modelos"
    except Exception as e:  # noqa: BLE001
        return False, f"excepción:{type(e).__name__}"


def check_guards_block() -> tuple[bool, str]:
    """Contrato 3: los guards bloqueantes (migration_linter, phi_guard) BLOQUEAN."""
    try:
        r = subprocess.run(
            [PYBIN, "-m", "pytest", "-q", "-o", "addopts=", "-k", _GUARD_TESTS],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0
        tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or ["(sin salida)"]
        return ok, tail[0]
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"pytest no corrió:{e}"


def run(skip_route: bool = False) -> int:
    """Corre el gate completo; devuelve 0 si los contratos CORE pasan, 1 si no."""
    mem_ok, mem_d = check_memory()
    guards_ok, guards_d = check_guards_block()
    route_ok, route_d = (True, "omitido") if skip_route else check_local_route()

    core_ok = mem_ok and guards_ok  # ruta local = advisory, no tumba el gate
    print(f"{'✓' if mem_ok else '✗'} MEMORIA  · {mem_d}")
    print(f"{'✓' if guards_ok else '✗'} GUARDS   · {guards_d}")
    print(f"{'✓' if route_ok else '⚠'} RUTA-LOCAL · {route_d}")
    print(f"\nGATE: {'OK — seguro operar/auto-adaptar' if core_ok else 'FALLO — NO auto-adaptar'}")

    _log({"ts": datetime.now(UTC).isoformat(), "event": "contract_gate",
          "core_ok": core_ok, "memory": mem_ok, "guards": guards_ok, "route": route_ok,
          "detail": {"memory": mem_d, "guards": guards_d, "route": route_d}})
    return 0 if core_ok else 1


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada CLI."""
    ap = argparse.ArgumentParser(description="Gate de contrato pre-auto-adaptación de ARIS4U")
    ap.add_argument("--skip-route", action="store_true",
                    help="omitir el check del cuerpo local (lazy/offline)")
    args = ap.parse_args(argv)
    return run(skip_route=args.skip_route)


if __name__ == "__main__":
    raise SystemExit(main())
