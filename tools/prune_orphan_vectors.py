#!/usr/bin/env python3
"""Poda vectores huérfanos del sidecar semántico (data/aris_vectors.db).

Un vector es HUÉRFANO si su (source, source_id) ya no existe en la fuente real.
Causa raíz: sessions.db fue purgada de fixtures de test (1375+1259 filas), pero el
sidecar nunca se des-indexó — ``vector_store.delete_item`` existía SIN callers. Resultado
medido 2026-06-16: 1370 vectores-decision apuntando a decisiones inexistentes, que
diluyen el top-k del KNN (orphan hits hidratan vacío y se descartan en silencio).

Este script ES el caller que faltaba para ``delete_item``. Córrelo tras cualquier purga
de sessions.db.

Uso:
    python3 tools/prune_orphan_vectors.py            # dry-run (default): solo reporta
    python3 tools/prune_orphan_vectors.py --apply    # backup (.backup) + borra

Solo poda source='decisions' (cruzado vs sessions.db decisions.id). Las observations
(claude-mem.db, tool externo) se reportan pero NO se tocan.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.v16 import vector_store  # noqa: E402
from engine.v16.config import ARIS_VECTORS_DB, SESSIONS_DB  # noqa: E402


def _valid_decision_ids() -> set[str]:
    """IDs de decisiones que SÍ existen en sessions.db (id == rowid, INTEGER PK)."""
    con = sqlite3.connect(str(SESSIONS_DB))
    try:
        return {str(r[0]) for r in con.execute("SELECT id FROM decisions")}
    finally:
        con.close()


def _vecmap_by_source(source: str) -> list[str]:
    """source_id de todos los vectores de un source en el sidecar."""
    con = sqlite3.connect(str(ARIS_VECTORS_DB))
    try:
        return [r[0] for r in con.execute(
            "SELECT source_id FROM vec_map WHERE source=?", (source,))]
    finally:
        con.close()


def main() -> int:
    """Punto de entrada CLI."""
    ap = argparse.ArgumentParser(description="Poda vectores-decision huérfanos del sidecar")
    ap.add_argument("--apply", action="store_true",
                    help="borra de verdad (default: dry-run)")
    args = ap.parse_args()

    if not vector_store.available():
        print("sqlite-vec no disponible — no se puede podar vec_items.", file=sys.stderr)
        return 1

    valid = _valid_decision_ids()
    vec_ids = _vecmap_by_source("decisions")
    orphans = [sid for sid in vec_ids if sid not in valid]
    obs = _vecmap_by_source("observations")

    print(f"decisions válidas en sessions.db : {len(valid)}")
    print(f"vec_map source='decisions'       : {len(vec_ids)}")
    print(f"HUÉRFANOS (sin decisión)         : {len(orphans)}")
    print(f"vec_map source='observations'    : {len(obs)} (no se tocan: DB externa claude-mem)")

    if not orphans:
        print("\nNada que podar. ✅")
        return 0
    if not args.apply:
        print(f"\n[DRY-RUN] borraría {len(orphans)} vectores-decision huérfanos. "
              f"Re-corre con --apply.")
        return 0

    # Backup antes de mutar: sqlite3 .backup (nunca cp — regla A3 del pre-mortem).
    bak = ARIS_VECTORS_DB.with_name(ARIS_VECTORS_DB.name + ".bak-prune-20260616")
    src = sqlite3.connect(str(ARIS_VECTORS_DB))
    dst = sqlite3.connect(str(bak))
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"\nbackup: {bak}")

    deleted = sum(1 for sid in orphans if vector_store.delete_item("decisions", sid))
    after = [sid for sid in _vecmap_by_source("decisions") if sid not in valid]
    print(f"BORRADOS: {deleted}/{len(orphans)} · huérfanos restantes: {len(after)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
