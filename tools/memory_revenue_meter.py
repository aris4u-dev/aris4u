#!/usr/bin/env python3
"""Medidor de memoria-revenue para ARIS4U — instrumento del falsador 2026-10-03.

DIMENSIÓN MEDIDA: composición del corpus ALMACENADO en sessions.db
(decisions + guards + digests + observations_local clasificados por bucket de cliente).

DISTINCIÓN IMPORTANTE vs herramientas existentes:
- ``recall_usefulness.py``   mide UTILIDAD de lo RECUPERADO en sesión.
- ``revenue_ledger.py``      mide M1-M5 (M1 = decisions solo, read-only, sin tendencia).
- ESTE módulo               mide COMPOSICIÓN de TODO lo almacenado, con snapshots de
                            tendencia para comparar corrida a corrida.

OBJETIVO ESTRATÉGICO: ratio memoria-revenue ≥ 40% de la memoria taggeada al 2026-10-03.
Baseline medido: ver snapshot generado al correr por primera vez.

Uso:
    python -m tools.memory_revenue_meter
    python -m tools.memory_revenue_meter --json
    python -m tools.memory_revenue_meter --no-snapshot   # no persiste, solo lee
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuración — buckets de cliente (ajustar con los valores REALES de la DB)
# ---------------------------------------------------------------------------

REVENUE_CLIENTS: frozenset[str] = frozenset(
    {"client-b", "client-b-platform", "client-c", "client-d", "client-e", "client-a", "acme-wellness"}
)
SELF_CLIENTS: frozenset[str] = frozenset({"aris4u", "lab-project-3"})
LAB_CLIENTS: frozenset[str] = frozenset(
    {"lab-project-1", "lab-project-1-legacy", "lab-project-2", "lab-project-4", "quimera"}
)
PENTEST_CLIENTS: frozenset[str] = frozenset({"pentest"})

# Tablas de memoria y la columna de cliente en cada una.
MEMORY_TABLES: tuple[str, ...] = (
    "decisions",
    "guards",
    "digests",
    "observations_local",
)

# Constantes del falsador estratégico.
TARGET_RATIO: float = 0.40  # revenue / tagged ≥ 40 %
DEADLINE: str = "2026-10-03"
_DEADLINE_DATE: date = date(2026, 10, 3)

_ARIS_ROOT = Path(__file__).resolve().parent.parent
_DB_PATH = _ARIS_ROOT / "data" / "sessions.db"

# Nombre del bucket "sin cliente asignado" (NULL o string vacío).
BUCKET_NULL = "NULL"
BUCKET_OTHER = "other"
BUCKET_REVENUE = "revenue"
BUCKET_SELF = "self"
BUCKET_LAB = "lab"
BUCKET_PENTEST = "pentest"


# ---------------------------------------------------------------------------
# Lógica pura de bucketing (sin IO — testeables en aislamiento)
# ---------------------------------------------------------------------------


def classify_client(client_id: str | None) -> str:
    """Clasifica un ``client_id`` de la DB en uno de los 6 buckets.

    Args:
        client_id: Valor crudo de la columna ``client_id`` (puede ser None o '').

    Returns:
        Una de las constantes de bucket: 'revenue', 'self', 'lab', 'pentest',
        'other', 'NULL'.
    """
    if client_id is None or client_id == "":
        return BUCKET_NULL
    cid = client_id.strip().lower()
    if not cid:
        return BUCKET_NULL
    if cid in REVENUE_CLIENTS:
        return BUCKET_REVENUE
    if cid in SELF_CLIENTS:
        return BUCKET_SELF
    if cid in LAB_CLIENTS:
        return BUCKET_LAB
    if cid in PENTEST_CLIENTS:
        return BUCKET_PENTEST
    return BUCKET_OTHER


def compute_ratio(
    counts_by_bucket: dict[str, int],
) -> tuple[float, float]:
    """Calcula los dos ratios de memoria-revenue a partir de conteos por bucket.

    Args:
        counts_by_bucket: Mapa bucket → número de filas (incluye BUCKET_NULL).

    Returns:
        Tupla ``(ratio_tagged, ratio_total)``  donde:
        - ``ratio_tagged``: revenue / (total - NULL)  — el KPI principal.
        - ``ratio_total``:  revenue / total            — métrica secundaria.
        Ambos son floats en [0, 1]; 0.0 si el denominador es 0.
    """
    total = sum(counts_by_bucket.values())
    null_n = counts_by_bucket.get(BUCKET_NULL, 0)
    revenue_n = counts_by_bucket.get(BUCKET_REVENUE, 0)
    tagged = total - null_n
    ratio_tagged = revenue_n / tagged if tagged > 0 else 0.0
    ratio_total = revenue_n / total if total > 0 else 0.0
    return ratio_tagged, ratio_total


def gap_to_target(ratio_tagged: float) -> float:
    """Diferencia (TARGET_RATIO - ratio_tagged); negativo = ya superó el target.

    Args:
        ratio_tagged: Ratio actual revenue/tagged (float en [0,1]).

    Returns:
        Gap en puntos de ratio (e.g. 0.33 = falta 33pp).
    """
    return TARGET_RATIO - ratio_tagged


def days_to_deadline() -> int:
    """Días naturales desde hoy hasta DEADLINE (calculado en tiempo real, no hardcodeado).

    Returns:
        Número de días restantes; negativo si ya pasó el deadline.
    """
    return (_DEADLINE_DATE - date.today()).days


# ---------------------------------------------------------------------------
# Capa de acceso a la DB (read + write snapshot)
# ---------------------------------------------------------------------------


def _query_table_counts(
    conn: sqlite3.Connection, table: str
) -> dict[str, int]:
    """Lee conteos agrupados por client_id para una tabla de memoria.

    Args:
        conn: Conexión SQLite.
        table: Nombre de la tabla (debe estar en MEMORY_TABLES).

    Returns:
        Mapa ``{client_id_raw: count}``; el cliente puede ser None o ''.
    """
    rows = conn.execute(
        f"SELECT client_id, COUNT(*) FROM {table} GROUP BY client_id"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def collect_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    """Recopila conteos por ``(tabla, bucket)`` de todas las tablas de memoria.

    Args:
        conn: Conexión SQLite abierta a sessions.db.

    Returns:
        Mapa ``{tabla: {bucket: count}}``; todas las tablas siempre presentes.
    """
    result: dict[str, dict[str, int]] = {}
    for tbl in MEMORY_TABLES:
        raw = _query_table_counts(conn, tbl)
        bucket_counts: dict[str, int] = {}
        for client_id, cnt in raw.items():
            bucket = classify_client(client_id)
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + cnt
        result[tbl] = bucket_counts
    return result


def aggregate_buckets(
    table_counts: dict[str, dict[str, int]]
) -> dict[str, int]:
    """Agrega los conteos de todas las tablas en un único mapa bucket→total.

    Args:
        table_counts: Salida de ``collect_counts``.

    Returns:
        Mapa bucket → suma a través de todas las tablas.
    """
    totals: dict[str, int] = {}
    for bucket_map in table_counts.values():
        for bucket, cnt in bucket_map.items():
            totals[bucket] = totals.get(bucket, 0) + cnt
    return totals


# ---------------------------------------------------------------------------
# Persistencia de snapshots (tendencia)
# ---------------------------------------------------------------------------

_SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS memory_revenue_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT    NOT NULL,
    ratio_tagged     REAL    NOT NULL,
    ratio_total      REAL    NOT NULL,
    counts_json      TEXT    NOT NULL,
    bucket_totals_json TEXT  NOT NULL
)
"""


def ensure_snapshot_table(conn: sqlite3.Connection) -> None:
    """Crea la tabla de snapshots si no existe (idempotente).

    Args:
        conn: Conexión SQLite con permisos de escritura.
    """
    conn.execute(_SNAPSHOT_DDL)
    conn.commit()


def save_snapshot(
    conn: sqlite3.Connection,
    ratio_tagged: float,
    ratio_total: float,
    table_counts: dict[str, dict[str, int]],
    bucket_totals: dict[str, int],
) -> int:
    """Inserta un snapshot en ``memory_revenue_snapshots``.

    Args:
        conn: Conexión SQLite con permisos de escritura.
        ratio_tagged: Ratio revenue/tagged actual.
        ratio_total: Ratio revenue/total actual.
        table_counts: Conteos por tabla y bucket.
        bucket_totals: Conteos agregados por bucket.

    Returns:
        ID de la fila insertada.
    """
    ensure_snapshot_table(conn)
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO memory_revenue_snapshots "
        "(ts, ratio_tagged, ratio_total, counts_json, bucket_totals_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            ts,
            ratio_tagged,
            ratio_total,
            json.dumps(table_counts, ensure_ascii=False),
            json.dumps(bucket_totals, ensure_ascii=False),
        ),
    )
    conn.commit()
    return cur.lastrowid or 0


def load_previous_snapshot(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Carga el snapshot más reciente previo al actual (penúltimo en orden temporal).

    Útil para calcular el delta entre la corrida actual y la anterior.
    Se llama ANTES de insertar el snapshot nuevo.

    Args:
        conn: Conexión SQLite.

    Returns:
        Dict con claves ts/ratio_tagged/ratio_total/counts_json/bucket_totals_json,
        o None si no hay ningún snapshot previo.
    """
    try:
        ensure_snapshot_table(conn)
        row = conn.execute(
            "SELECT ts, ratio_tagged, ratio_total, counts_json, bucket_totals_json "
            "FROM memory_revenue_snapshots ORDER BY ts DESC, id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    return {
        "ts": row[0],
        "ratio_tagged": row[1],
        "ratio_total": row[2],
        "counts_json": json.loads(row[3]),
        "bucket_totals_json": json.loads(row[4]),
    }


# ---------------------------------------------------------------------------
# Construcción del reporte
# ---------------------------------------------------------------------------


@dataclass
class MeterReport:
    """Resultado completo de una corrida del medidor."""

    run_ts: str
    deadline: str
    days_to_deadline: int
    ratio_tagged: float
    ratio_total: float
    gap_to_target: float
    target_ratio: float
    bucket_totals: dict[str, int]
    table_counts: dict[str, dict[str, int]]
    prev_snapshot: dict[str, Any] | None = field(default=None, repr=False)
    snapshot_id: int | None = None

    @property
    def delta_ratio_tagged(self) -> float | None:
        """Cambio en ratio_tagged respecto al snapshot anterior (None si no hay)."""
        if self.prev_snapshot is None:
            return None
        return self.ratio_tagged - self.prev_snapshot["ratio_tagged"]

    def as_dict(self) -> dict[str, Any]:
        """Serializa el reporte a un dict JSON-serializable."""
        d = asdict(self)
        d["delta_ratio_tagged"] = self.delta_ratio_tagged
        return d


def build_report(
    db: Path | None = None,
    save: bool = True,
) -> MeterReport:
    """Ejecuta el medidor completo: lee DB, calcula ratios, persiste snapshot.

    Args:
        db: Ruta a sessions.db; usa ``_DB_PATH`` si es None.
        save: Si True (default), persiste el snapshot de tendencia.

    Returns:
        ``MeterReport`` con todos los datos de la corrida.

    Raises:
        FileNotFoundError: Si sessions.db no existe.
    """
    db_path = db or _DB_PATH
    if not db_path.exists():
        raise FileNotFoundError(f"sessions.db no encontrada: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        table_counts = collect_counts(conn)
        bucket_totals = aggregate_buckets(table_counts)
        ratio_tagged, ratio_total = compute_ratio(bucket_totals)
        prev = load_previous_snapshot(conn) if save else None
        snap_id: int | None = None
        if save:
            snap_id = save_snapshot(conn, ratio_tagged, ratio_total, table_counts, bucket_totals)
    finally:
        conn.close()

    return MeterReport(
        run_ts=datetime.now(UTC).isoformat(timespec="seconds"),
        deadline=DEADLINE,
        days_to_deadline=days_to_deadline(),
        ratio_tagged=round(ratio_tagged, 6),
        ratio_total=round(ratio_total, 6),
        gap_to_target=round(gap_to_target(ratio_tagged), 6),
        target_ratio=TARGET_RATIO,
        bucket_totals=bucket_totals,
        table_counts=table_counts,
        prev_snapshot=prev,
        snapshot_id=snap_id,
    )


# ---------------------------------------------------------------------------
# Formateo de salida legible
# ---------------------------------------------------------------------------

_BAR_WIDTH = 24


def _bar(ratio: float, width: int = _BAR_WIDTH) -> str:
    """Barra de progreso ASCII para un ratio [0,1]."""
    filled = round(ratio * width)
    filled = max(0, min(filled, width))
    pct = ratio * 100
    return f"[{'#' * filled}{'.' * (width - filled)}] {pct:5.1f}%"


def _delta_str(delta: float | None) -> str:
    if delta is None:
        return "(sin snapshot previo)"
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta * 100:.2f}pp vs snapshot anterior"


def print_report(r: MeterReport) -> None:
    """Imprime el reporte del medidor en formato legible para terminal.

    Args:
        r: Resultado completo de ``build_report``.
    """
    W = 68
    sep = "-" * W
    total_all = sum(r.bucket_totals.values())
    null_n = r.bucket_totals.get(BUCKET_NULL, 0)
    tagged = total_all - null_n

    print()
    print("=" * W)
    print("  ARIS4U — MEDIDOR DE MEMORIA-REVENUE")
    print(f"  {r.run_ts}   Deadline: {r.deadline}  ({r.days_to_deadline}d)")
    print("=" * W)

    print()
    print(f"  RATIO REVENUE/TAGGED  {_bar(r.ratio_tagged)}")
    print(f"  RATIO REVENUE/TOTAL   {_bar(r.ratio_total)}")
    print(f"  Target: {r.target_ratio * 100:.0f}%  |  Gap: {r.gap_to_target * 100:+.2f}pp  |  "
          f"Delta: {_delta_str(r.delta_ratio_tagged)}")

    print()
    print(sep)
    print("  BREAKDOWN POR BUCKET (agregado · todas las tablas)")
    print(sep)
    bucket_order = [BUCKET_REVENUE, BUCKET_SELF, BUCKET_LAB, BUCKET_PENTEST, BUCKET_OTHER, BUCKET_NULL]
    for b in bucket_order:
        n = r.bucket_totals.get(b, 0)
        pct_total = 100.0 * n / total_all if total_all else 0.0
        pct_tagged = 100.0 * n / tagged if (tagged and b != BUCKET_NULL) else None
        tag_str = f"  ({pct_tagged:5.1f}% de taggeados)" if pct_tagged is not None else ""
        print(f"  {b:<12}  {n:>6} de {total_all:<6}  ({pct_total:5.1f}% del total){tag_str}")

    print()
    print(sep)
    print("  BREAKDOWN POR TABLA")
    print(sep)
    for tbl in MEMORY_TABLES:
        bc = r.table_counts.get(tbl, {})
        tbl_total = sum(bc.values())
        rev_n = bc.get(BUCKET_REVENUE, 0)
        null_tbl = bc.get(BUCKET_NULL, 0)
        tagged_tbl = tbl_total - null_tbl
        ratio_tbl = rev_n / tagged_tbl if tagged_tbl else 0.0
        print(
            f"  {tbl:<22}  total={tbl_total:<5}  revenue={rev_n:<4} de {tagged_tbl:<4} taggeados  "
            f"({ratio_tbl * 100:.1f}%)"
        )

    if r.snapshot_id is not None:
        print()
        print(f"  Snapshot #{r.snapshot_id} guardado en memory_revenue_snapshots.")
    print()
    print("=" * W)
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Medidor de memoria-revenue ARIS4U — instrumento falsador 2026-10-03"
    )
    ap.add_argument("--json", action="store_true", help="Salida JSON (máquina)")
    ap.add_argument(
        "--no-snapshot",
        action="store_true",
        help="No persiste snapshot (solo lectura)",
    )
    ap.add_argument("--db", type=Path, default=None, help="Ruta a sessions.db")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada CLI.

    Args:
        argv: Argumentos de línea de comandos (None = sys.argv).

    Returns:
        Código de salida (0 = ok, 1 = error).
    """
    args = _parse_args(argv)
    try:
        report = build_report(db=args.db, save=not args.no_snapshot)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
