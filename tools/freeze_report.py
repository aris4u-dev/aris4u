#!/usr/bin/env python3
"""Reporte de viernes del FREEZE — convierte la telemetría auto_recall en la métrica.

Ítem 0 del plan de freeze (ver architecture/ARIS4U_MASTER.md §7). Lee el log enriquecido
`logs/v16.1-events.jsonl` y computa, por ventana semanal:

  - Leading indicator (implícito, computable hoy): recalls con-resultados/semana, % vacío,
    desglose A/B (raw vs condensed), por cliente, latencia p50, % con n_semantic==0
    (diagnóstico del ~50% de vacíos: ¿es el lado semántico/Ollama o queries sin match?).
  - Métrica primaria (verdad humana): "recalls útiles/semana", de marcado batch en el
    ritual del viernes (--review lista candidatos; --mark persiste el juicio).

Uso:
    python3 tools/freeze_report.py [--days N]        # reporte de la ventana (default 7)
    python3 tools/freeze_report.py --review [--days N]  # lista recalls con-resultados sin marcar
    python3 tools/freeze_report.py --mark RECALL_ID 1   # marca útil (1) / no-útil (0)

El umbral del freeze: éxito = >=3 recalls ÚTILES/semana sostenido 2 semanas; <1 = re-diagnosticar.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone, UTC
from pathlib import Path
from statistics import median


def _root() -> Path:
    """Resuelve ARIS4U_ROOT (env, o la raíz del repo desde este archivo)."""
    env = os.environ.get("ARIS4U_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def _events(log_path: Path, since: datetime) -> list[dict]:
    """Lee eventos auto_recall del jsonl dentro de la ventana [since, now].

    Args:
        log_path: Ruta al log JSONL enriquecido.
        since: Límite inferior temporal (UTC, aware).

    Returns:
        Lista de eventos auto_recall (dicts) con ts >= since.
    """
    out: list[dict] = []
    if not log_path.exists():
        return out
    with log_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line or '"auto_recall"' not in line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("event") != "auto_recall":
                continue
            ts_raw = ev.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except (ValueError, TypeError):
                continue
            if ts >= since:
                ev["_ts"] = ts
                out.append(ev)
    return out


def _feedback_db(db_path: Path) -> sqlite3.Connection:
    """Abre sessions.db y asegura la tabla recall_feedback (marcado de utilidad)."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS recall_feedback ("
        "recall_id TEXT PRIMARY KEY, useful INTEGER NOT NULL, "
        "marked_at TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def _p50_latency(events: list[dict]) -> int:
    """Latencia mediana (ms) de los eventos; 0 si no hay datos."""
    lats = [e.get("latency_ms", 0) for e in events if isinstance(e.get("latency_ms"), int)]
    return int(median(lats)) if lats else 0


def _print_summary(events: list[dict], days: int) -> None:
    """Imprime el encabezado, la tasa con-resultados/vacíos y la latencia p50.

    Args:
        events: Eventos auto_recall de la ventana (no vacío).
        days: Tamaño de la ventana en días (para el encabezado).
    """
    total = len(events)
    with_results = sum(1 for e in events if e.get("results", 0) > 0)
    empty = total - with_results
    print(f"Con resultados: {with_results}/{total} "
          f"({100*with_results//total}%) · vacíos: {empty} ({100*empty//total}%)")
    print(f"Latencia p50: {_p50_latency(events)} ms")


def _print_format_ab(events: list[dict]) -> None:
    """Imprime el desglose A/B por formato (crudo vs condensado).

    Args:
        events: Eventos auto_recall de la ventana.
    """
    by_fmt: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_fmt[e.get("format", "legacy")].append(e)
    print("\n-- A/B de formato (crudo vs condensado) --")
    for fmt, evs in sorted(by_fmt.items()):
        wr = sum(1 for e in evs if e.get("results", 0) > 0)
        pct = 100 * wr // len(evs) if evs else 0
        print(f"  {fmt:10s}: {len(evs):4d} recalls · con-resultados {wr} ({pct}%)")


def _print_by_client(events: list[dict]) -> None:
    """Imprime el conteo de recalls por cliente, descendente.

    Args:
        events: Eventos auto_recall de la ventana.
    """
    by_client: Counter = Counter(e.get("client", "") or "(sin cliente)" for e in events)
    print("\n-- Por cliente --")
    for cli, n in by_client.most_common():
        print(f"  {cli:20s}: {n}")


def _print_vacancy_diagnostic(events: list[dict]) -> None:
    """Imprime el diagnóstico del vacío (n_semantic==0) si hay desglose.

    Args:
        events: Eventos auto_recall de la ventana.
    """
    has_breakdown = sum(1 for e in events if "n_semantic" in e)
    if not has_breakdown:
        return
    sem_zero = sum(1 for e in events if e.get("n_semantic", None) == 0)
    print(f"\n-- Diagnóstico del vacío -- (eventos con desglose: {has_breakdown})")
    print(f"  n_semantic==0 (Ollama/embeddings no aportó): {sem_zero}/{has_breakdown} "
          f"({100*sem_zero//has_breakdown if has_breakdown else 0}%)")


def _print_empty_queries(events: list[dict]) -> None:
    """Imprime la muestra de queries que salieron vacías, si las hay.

    Args:
        events: Eventos auto_recall de la ventana.
    """
    empty_q = [e.get("query", "") for e in events
               if e.get("results", 0) == 0 and e.get("query")]
    if not empty_q:
        return
    print("\n-- Top queries que salieron VACÍAS (muestra) --")
    for q, n in Counter(empty_q).most_common(8):
        print(f"  ({n}x) {q[:90]}")


def _print_primary_metric(
    events: list[dict], conn: sqlite3.Connection, days: int
) -> None:
    """Imprime la métrica primaria: recalls ÚTILES por marcado humano.

    Args:
        events: Eventos auto_recall de la ventana.
        conn: Conexión a sessions.db con la tabla recall_feedback.
        days: Tamaño de la ventana, para normalizar a útiles/semana.
    """
    marked = dict(conn.execute("SELECT recall_id, useful FROM recall_feedback").fetchall())
    rids = {e.get("recall_id") for e in events if e.get("recall_id")}
    useful = sum(1 for r in rids if marked.get(r) == 1)
    not_useful = sum(1 for r in rids if marked.get(r) == 0)
    unmarked = sum(1 for r in rids if r not in marked)
    weeks = max(1, days / 7)
    print("\n=== MÉTRICA PRIMARIA: recalls ÚTILES (marcado humano) ===")
    print(f"  útiles: {useful} · no-útiles: {not_useful} · sin marcar: {unmarked}")
    print(f"  útiles/semana ≈ {useful/weeks:.1f}  "
          f"(umbral éxito >=3 sostenido 2 sem; <1 = re-diagnosticar)")
    if unmarked:
        print(f"  → {unmarked} sin marcar. Corre: python3 tools/freeze_report.py --review")


def cmd_report(events: list[dict], conn: sqlite3.Connection, days: int) -> None:
    """Imprime el reporte semanal (leading + métrica primaria)."""
    total = len(events)
    print(f"\n=== REPORTE FREEZE · últimos {days} días · {total} auto_recalls ===")
    if total == 0:
        print("Sin eventos en la ventana. ¿Hubo sesiones? (revisar SessionStart/hook)")
        return
    _print_summary(events, days)
    _print_format_ab(events)
    _print_by_client(events)
    _print_vacancy_diagnostic(events)
    _print_empty_queries(events)
    _print_primary_metric(events, conn, days)


def cmd_review(events: list[dict], conn: sqlite3.Connection) -> None:
    """Lista recalls con-resultados aún sin marcar (para el ritual del viernes)."""
    marked = {r for (r,) in conn.execute("SELECT recall_id FROM recall_feedback").fetchall()}
    cand = [e for e in events if e.get("results", 0) > 0
            and e.get("recall_id") and e["recall_id"] not in marked]
    if not cand:
        print("No hay recalls con-resultados sin marcar en la ventana. ✅")
        return
    print(f"\n{len(cand)} recalls sin marcar. Marca con: "
          f"python3 tools/freeze_report.py --mark <recall_id> <1|0>\n")
    for e in cand[-30:]:
        cli = e.get("client", "") or "-"
        print(f"  [{e.get('recall_id')}] ({cli}, {e.get('format','?')}, "
              f"{e.get('results')} res) {e.get('query','')[:80]}")


def cmd_mark(conn: sqlite3.Connection, recall_id: str, useful: int) -> None:
    """Persiste el juicio de utilidad de un recall."""
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO recall_feedback (recall_id, useful, marked_at) VALUES (?,?,?) "
        "ON CONFLICT(recall_id) DO UPDATE SET useful=excluded.useful, marked_at=excluded.marked_at",
        (recall_id, 1 if useful else 0, now),
    )
    conn.commit()
    print(f"Marcado {recall_id} → {'ÚTIL' if useful else 'no-útil'}")


def main() -> int:
    """Punto de entrada CLI."""
    ap = argparse.ArgumentParser(description="Reporte de viernes del freeze ARIS4U")
    ap.add_argument("--days", type=int, default=7, help="ventana en días (default 7)")
    ap.add_argument("--review", action="store_true", help="listar recalls sin marcar")
    ap.add_argument("--mark", nargs=2, metavar=("RECALL_ID", "USEFUL"),
                    help="marcar utilidad: <recall_id> <1|0>")
    args = ap.parse_args()

    root = _root()
    log_path = root / "logs" / "v16.1-events.jsonl"
    db_path = root / "data" / "sessions.db"
    if not db_path.exists():
        print(f"sessions.db no encontrada en {db_path}", file=sys.stderr)
        return 1
    conn = _feedback_db(db_path)

    if args.mark:
        cmd_mark(conn, args.mark[0], int(args.mark[1]))
        return 0

    since = datetime.now(UTC) - timedelta(days=args.days)
    events = _events(log_path, since)
    if args.review:
        cmd_review(events, conn)
    else:
        cmd_report(events, conn, args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
