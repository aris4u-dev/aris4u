#!/usr/bin/env python3
"""Migración one-time: texto de claude-mem → observations_local propia (V18 Fase E, paso 2).

Copia el TEXTO de las observations que el sidecar aris_vectors.db referencia (source_id de
vec_map) desde ~/.claude-mem/claude-mem.db a la tabla propia `observations_local` en
sessions.db. Preserva PARIDAD: una fila por vector (por `id`), sin dedup por content_hash
(el histórico tiene 26% de hashes duplicados que aún deben hidratar). Idempotente:
INSERT OR REPLACE por id → re-correr no duplica.

Uso:
    python3 tools/migrate_observations_local.py            # migra
    python3 tools/migrate_observations_local.py --verify   # solo cuenta paridad, no escribe
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(os.environ.get("ARIS4U_ROOT") or Path(__file__).resolve().parents[1])
SESSIONS_DB = ROOT / "data" / "sessions.db"
VECTORS_DB = ROOT / "data" / "aris_vectors.db"
CLAUDE_MEM = Path(os.path.expanduser("~/.claude-mem/claude-mem.db"))

# Texto que _hydrate() usa: COALESCE(text, narrative, title).
_TEXT_EXPR = "COALESCE(NULLIF(text,''), NULLIF(narrative,''), title, '')"


def _referenced_ids() -> list[str]:
    v = sqlite3.connect(f"file:{VECTORS_DB}?mode=ro", uri=True)
    try:
        return [str(r[0]) for r in v.execute(
            "SELECT DISTINCT source_id FROM vec_map WHERE source='observations'")]
    finally:
        v.close()


def migrate(verify_only: bool = False) -> int:
    if not CLAUDE_MEM.is_file():
        print(f"⚠️ claude-mem.db no existe en {CLAUDE_MEM} — nada que migrar", file=sys.stderr)
        return 1
    ids = _referenced_ids()
    print(f"vectores source=observations a respaldar: {len(ids)}")

    cm = sqlite3.connect(f"file:{CLAUDE_MEM}?mode=ro", uri=True)
    sd = sqlite3.connect(str(SESSIONS_DB))
    sd.execute("PRAGMA busy_timeout = 10000")
    migrated = 0
    try:
        # Asegura el schema (crea observations_local si init_db no corrió aún).
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from engine.v16 import session_manager
        session_manager.init_db()

        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            qm = ",".join("?" * len(batch))
            rows = cm.execute(
                f"SELECT id, project, type, {_TEXT_EXPR}, content_hash, created_at, "
                f"verify_score, client_id FROM observations WHERE id IN ({qm})", batch
            ).fetchall()
            if verify_only:
                migrated += len(rows)
                continue
            sd.executemany(
                "INSERT OR REPLACE INTO observations_local "
                "(id, project, type, content, content_hash, created_at, verify_score, client_id) "
                "VALUES (?,?,?,?,?,?,?,?)", rows
            )
            migrated += len(rows)
        if not verify_only:
            sd.commit()
    finally:
        cm.close()
        sd.close()

    # Verificación de paridad.
    sd = sqlite3.connect(f"file:{SESSIONS_DB}?mode=ro", uri=True)
    try:
        local = sd.execute("SELECT COUNT(*) FROM observations_local").fetchone()[0]
    except sqlite3.OperationalError:
        local = 0
    finally:
        sd.close()
    print(f"{'[verify] halladas' if verify_only else 'migradas'}: {migrated}  ·  "
          f"observations_local ahora: {local}  ·  referenciadas: {len(ids)}")
    print("✅ PARIDAD OK" if (migrated == len(ids)) else "⚠️ faltan filas — revisar")
    return 0 if migrated == len(ids) else 2


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Migra texto de claude-mem → observations_local")
    ap.add_argument("--verify", action="store_true", help="solo cuenta paridad, no escribe")
    ns = ap.parse_args(argv)
    return migrate(verify_only=ns.verify)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
