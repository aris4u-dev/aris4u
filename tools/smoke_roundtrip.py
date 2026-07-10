#!/usr/bin/env python3
"""Smoke roundtrip de memoria de ARIS4U — prueba ACTIVA diaria del write-path.

FREEZE 4 semanas (ítem 1, §7 del MASTER) · defensa del modo de fallo #3 (write-path
roto en silencio, ya ocurrido 2×: `session_end` descableado 7 días; ingest `locked=1`
→ 0/1503 recuperables). Un backup o una métrica sobre un write-path muerto miden
basura; este check es el "seguro de validez" del experimento de 4 semanas.

Qué prueba (roundtrip COMPLETO, no "el hook corrió"):
  1. ESCRIBE una decisión sintética en `sessions.db` por el camino estructurado real
     (mismo INSERT que `session_manager.save_decision`; el trigger `decisions_ai`
     puebla `decisions_fts`).
  2. RECUPERA por el camino de lectura real (`session_manager.search`, FTS5).
  3. ASSERT que el token único vuelve. Si no vuelve → el roundtrip está roto.
  4. LIMPIA siempre (finally): borra la fila sintética + su entrada FTS. Cero
     contaminación de la memoria real (no usa el embed async → no ensucia el vector
     store; `domain='__smoke__'` es inequívoco para el cleanup).

Resultado:
  - Append a `logs/smoke_roundtrip.jsonl` (consumible por `freeze_report.py` y el
    reporte de viernes).
  - Exit code 0 = roundtrip vivo · 1 = roto (para el cron y cualquier monitor).

Uso:  python3 tools/smoke_roundtrip.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone, UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DB = ROOT / "data" / "sessions.db"
LOG = ROOT / "logs" / "smoke_roundtrip.jsonl"
SMOKE_DOMAIN = "__smoke__"


def _connect() -> sqlite3.Connection:
    """Misma configuración que session_manager._connect (WAL, busy_timeout)."""
    db = sqlite3.connect(str(SESSIONS_DB), timeout=10)
    db.execute("PRAGMA busy_timeout = 5000")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA synchronous = NORMAL")
    db.row_factory = sqlite3.Row
    return db


def _log(event: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def run() -> int:
    token = f"SMOKE{uuid.uuid4().hex[:12].upper()}"  # único, sin guion (FTS lo trata como 1 término)
    decision_text = f"smoke roundtrip probe {token}"
    rowid = None
    db = None
    ok = False
    detail = ""
    try:
        db = _connect()
        # 1. ESCRIBE por el camino estructurado real (el trigger decisions_ai puebla el FTS).
        cur = db.execute(
            "INSERT INTO decisions (decision, rationale, domain, locked) VALUES (?, ?, ?, 0)",
            (decision_text, "smoke test — auto-cleanup", SMOKE_DOMAIN),
        )
        rowid = cur.lastrowid
        db.commit()

        # 2. RECUPERA por el camino de lectura real (FTS5), en conexión nueva (lectura honesta).
        sys.path.insert(0, str(ROOT))
        from engine.v16.session_manager import search  # noqa: E402

        results = search(token, limit=5)
        hits = [d for d in results.get("decisions", []) if token in (d.get("decision") or "")]
        ok = bool(hits)
        detail = "recovered" if ok else "write_succeeded_but_not_recalled"
    except Exception as exc:  # noqa: BLE001 — el smoke nunca debe lanzar; reporta y sale 1
        ok = False
        detail = f"exception:{type(exc).__name__}:{exc}"
    finally:
        # 4. LIMPIA SIEMPRE: fila sintética + entrada FTS (no hay trigger de delete).
        if db is not None:
            try:
                if rowid is not None:
                    db.execute("DELETE FROM decisions_fts WHERE rowid = ?", (rowid,))
                db.execute("DELETE FROM decisions WHERE domain = ?", (SMOKE_DOMAIN,))
                db.commit()
            except Exception:
                pass
            try:
                db.close()
            except Exception:
                pass

    _log({
        "ts": datetime.now(UTC).isoformat(),
        "event": "smoke_roundtrip",
        "ok": ok,
        "detail": detail,
        "token": token,
    })
    if not ok:
        print(f"SMOKE ROUNDTRIP FAILED: {detail}", file=sys.stderr)
        return 1
    print(f"smoke roundtrip OK ({token})")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
