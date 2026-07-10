#!/usr/bin/env python3
"""Ledger multiplicador ARIS4U — dashboard semanal de progreso revenue.

Mide si el reencuadre "amplificador-revenue" funciona contra las métricas
del falsador de 90 días. Read-only sobre sessions.db; cero mutación.

Métricas:
  M1  Auto-referencia vs revenue (decisions.client_id)
  M2  Recall fill-rate por cliente (recall_events.client)
  M3  Recall útil global y en sesiones revenue (recall_feedback)
  M4  Decisiones por cliente revenue (decisions por client_id)
  M5  Guard-blocks que sirvieron a revenue (gate_results.session_ref)

Falsador: 2026-10-03. Baseline medido: 2026-07-05.

Uso:
    python3 tools/revenue_ledger.py
    python3 tools/revenue_ledger.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

ARIS_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ARIS_ROOT / "data" / "sessions.db"

# ── cliente buckets ────────────────────────────────────────────────────────────
SELF_CLIENTS: set[str] = {"aris4u", "lab-project-3"}
REVENUE_CLIENTS: list[str] = ["client-b", "client-c", "client-d", "client-a", "client-e"]

# ── baselines & targets ────────────────────────────────────────────────────────
BASELINES: dict[str, Any] = {
    "m1_self_pct": 57.0,
    "m1_revenue_pct": 20.9,
    "m2_fill_pct": 4.5,
    "m3_useful_global_pct": 20.8,
    "m3_useful_revenue_pct": None,  # no medido en baseline
    "m4_client-b": 4,
    "m4_client-a": 2,
}
TARGETS: dict[str, Any] = {
    "m1_self_pct": 30.0,  # self < 30 %
    "m1_revenue_pct": 50.0,  # revenue ≥ 50 %
    "m2_fill_pct": 90.0,  # tag fill > 90 %
    "m3_useful_revenue_pct": 50.0,  # useful en revenue > 50 %
    "m4_client-b": 40,  # client-b ≥ 40 decisions
}
FALSADOR_DATE = date(2026, 10, 3)


# ── helpers ────────────────────────────────────────────────────────────────────


def _open_ro(db: Path) -> sqlite3.Connection:
    """Abre la DB en modo read-only; falla si no existe."""
    if not db.exists():
        raise FileNotFoundError(f"sessions.db not found: {db}")
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _pct(n: int, total: int) -> float:
    return round(100.0 * n / total, 1) if total else 0.0


def _verdict(current: float, baseline: float | None, target: float, higher_is_better: bool) -> str:
    """↑ / → / ↓ según dirección respecto al baseline."""
    if baseline is None:
        return "?"
    delta = current - baseline
    if higher_is_better:
        if delta > 1.0:
            return "↑"
        if delta < -1.0:
            return "↓"
        return "→"
    else:  # lower is better (self%)
        if delta < -1.0:
            return "↑"
        if delta > 1.0:
            return "↓"
        return "→"


# ── métricas ───────────────────────────────────────────────────────────────────


@dataclass
class M1Result:
    total: int = 0
    self_n: int = 0
    revenue_n: int = 0
    other_n: int = 0
    total_30d: int = 0
    self_30d: int = 0
    revenue_30d: int = 0


def compute_m1(conn: sqlite3.Connection) -> M1Result:
    """M1 — auto-referencia vs revenue en decisions."""
    sql = """
        SELECT
            client_id,
            COUNT(*) AS cnt,
            SUM(CASE WHEN created_at >= datetime('now','-30 days') THEN 1 ELSE 0 END) AS cnt_30d
        FROM decisions
        GROUP BY client_id
    """
    r = M1Result()
    for row in conn.execute(sql):
        cid = row["client_id"] or ""
        n = row["cnt"]
        n30 = row["cnt_30d"]
        r.total += n
        r.total_30d += n30
        if cid in SELF_CLIENTS or cid == "":
            r.self_n += n
            r.self_30d += n30
        elif cid in REVENUE_CLIENTS:
            r.revenue_n += n
            r.revenue_30d += n30
        else:
            r.other_n += n
    return r


@dataclass
class M2Result:
    total_session_start: int = 0
    tagged_session_start: int = 0
    total_user_prompt: int = 0
    tagged_user_prompt: int = 0

    @property
    def total(self) -> int:
        return self.total_session_start + self.total_user_prompt

    @property
    def tagged(self) -> int:
        return self.tagged_session_start + self.tagged_user_prompt

    @property
    def fill_pct(self) -> float:
        return _pct(self.tagged, self.total)


def compute_m2(conn: sqlite3.Connection) -> M2Result:
    """M2 — recall fill-rate por cliente (recall_events.client)."""
    sql = """
        SELECT
            source,
            COUNT(*) AS total,
            SUM(CASE WHEN client != '' THEN 1 ELSE 0 END) AS tagged
        FROM recall_events
        GROUP BY source
    """
    r = M2Result()
    for row in conn.execute(sql):
        src = row["source"]
        tot = row["total"]
        tag = row["tagged"]
        if src == "session_start":
            r.total_session_start = tot
            r.tagged_session_start = tag
        elif src == "user_prompt":
            r.total_user_prompt = tot
            r.tagged_user_prompt = tag
    return r


@dataclass
class M3Result:
    total: int = 0
    useful: int = 0
    revenue_total: int = 0
    revenue_useful: int = 0

    @property
    def global_pct(self) -> float:
        return _pct(self.useful, self.total)

    @property
    def revenue_pct(self) -> float:
        return _pct(self.revenue_useful, self.revenue_total)


def compute_m3(conn: sqlite3.Connection) -> M3Result:
    """M3 — recall útil global y en sesiones revenue."""
    sql_global = "SELECT COUNT(*) AS t, SUM(useful) AS u FROM recall_feedback"
    row = conn.execute(sql_global).fetchone()
    r = M3Result(total=row["t"] or 0, useful=int(row["u"] or 0))

    rev_placeholders = ",".join("?" * len(REVENUE_CLIENTS))
    sql_rev = f"""
        SELECT COUNT(*) AS t, SUM(rf.useful) AS u
        FROM recall_feedback rf
        JOIN recall_events re ON rf.recall_id = re.recall_id
        WHERE re.client IN ({rev_placeholders})
    """
    row2 = conn.execute(sql_rev, REVENUE_CLIENTS).fetchone()
    r.revenue_total = row2["t"] or 0
    r.revenue_useful = int(row2["u"] or 0)
    return r


@dataclass
class M4Result:
    by_client: dict[str, int] = field(default_factory=dict)
    by_client_30d: dict[str, int] = field(default_factory=dict)


def compute_m4(conn: sqlite3.Connection) -> M4Result:
    """M4 — decisiones por cliente revenue (total y rolling 30d)."""
    rev_placeholders = ",".join("?" * len(REVENUE_CLIENTS))
    sql = f"""
        SELECT
            client_id,
            COUNT(*) AS cnt,
            SUM(CASE WHEN created_at >= datetime('now','-30 days') THEN 1 ELSE 0 END) AS cnt_30d
        FROM decisions
        WHERE client_id IN ({rev_placeholders})
        GROUP BY client_id
    """
    r = M4Result()
    for row in conn.execute(sql, REVENUE_CLIENTS):
        r.by_client[row["client_id"]] = row["cnt"]
        r.by_client_30d[row["client_id"]] = row["cnt_30d"]
    # ensure all revenue clients present (even if 0)
    for c in REVENUE_CLIENTS:
        r.by_client.setdefault(c, 0)
        r.by_client_30d.setdefault(c, 0)
    return r


@dataclass
class M5Result:
    signal_available: bool = False
    note: str = ""
    revenue_blocks: int = 0
    total_blocks: int = 0


def compute_m5(conn: sqlite3.Connection) -> M5Result:
    """M5 — guard-blocks con señal revenue en gate_results."""
    # Verificar si session_ref está vacío en gate_results
    row = conn.execute(
        "SELECT COUNT(*) AS t, "
        "SUM(CASE WHEN session_ref IS NOT NULL AND session_ref != '' THEN 1 ELSE 0 END) AS tagged "
        "FROM gate_results"
    ).fetchone()
    total = row["t"] or 0
    tagged = row["tagged"] or 0
    if tagged == 0:
        return M5Result(
            signal_available=False,
            note=(
                f"gate_results.session_ref vacío en {total} filas — "
                "instrumentar session_ref para activar esta métrica"
            ),
            total_blocks=total,
        )
    # Si hay señal futura, filtrar por revenue
    rev_placeholders = ",".join("?" * len(REVENUE_CLIENTS))
    row2 = conn.execute(
        f"SELECT COUNT(*) AS r FROM gate_results WHERE session_ref IN ({rev_placeholders})",
        REVENUE_CLIENTS,
    ).fetchone()
    return M5Result(
        signal_available=True,
        revenue_blocks=row2["r"] or 0,
        total_blocks=total,
    )


# ── report ─────────────────────────────────────────────────────────────────────


def build_report(db: Path | None = None) -> dict[str, Any]:
    conn = _open_ro(db or DB_PATH)
    try:
        m1 = compute_m1(conn)
        m2 = compute_m2(conn)
        m3 = compute_m3(conn)
        m4 = compute_m4(conn)
        m5 = compute_m5(conn)
    finally:
        conn.close()

    days_left = (FALSADOR_DATE - date.today()).days

    return {
        "run_date": datetime.now(UTC).isoformat(timespec="seconds"),
        "falsador_date": str(FALSADOR_DATE),
        "days_to_falsador": days_left,
        "m1": {
            "total_decisions": m1.total,
            "self_total": m1.self_n,
            "revenue_total": m1.revenue_n,
            "other_total": m1.other_n,
            "self_pct_total": _pct(m1.self_n, m1.total),
            "revenue_pct_total": _pct(m1.revenue_n, m1.total),
            "self_pct_30d": _pct(m1.self_30d, m1.total_30d),
            "revenue_pct_30d": _pct(m1.revenue_30d, m1.total_30d),
            "total_30d": m1.total_30d,
            "baseline_self": BASELINES["m1_self_pct"],
            "baseline_revenue": BASELINES["m1_revenue_pct"],
            "target_self": TARGETS["m1_self_pct"],
            "target_revenue": TARGETS["m1_revenue_pct"],
            "verdict_self": _verdict(
                _pct(m1.self_n, m1.total),
                BASELINES["m1_self_pct"],
                TARGETS["m1_self_pct"],
                higher_is_better=False,
            ),
            "verdict_revenue": _verdict(
                _pct(m1.revenue_n, m1.total),
                BASELINES["m1_revenue_pct"],
                TARGETS["m1_revenue_pct"],
                higher_is_better=True,
            ),
        },
        "m2": {
            "total": m2.total,
            "tagged": m2.tagged,
            "fill_pct": m2.fill_pct,
            "session_start_total": m2.total_session_start,
            "session_start_tagged": m2.tagged_session_start,
            "user_prompt_total": m2.total_user_prompt,
            "user_prompt_tagged": m2.tagged_user_prompt,
            "baseline": BASELINES["m2_fill_pct"],
            "target": TARGETS["m2_fill_pct"],
            "verdict": _verdict(
                m2.fill_pct, BASELINES["m2_fill_pct"], TARGETS["m2_fill_pct"], higher_is_better=True
            ),
        },
        "m3": {
            "total": m3.total,
            "useful": m3.useful,
            "global_pct": m3.global_pct,
            "revenue_total": m3.revenue_total,
            "revenue_useful": m3.revenue_useful,
            "revenue_pct": m3.revenue_pct,
            "baseline_global": BASELINES["m3_useful_global_pct"],
            "target_revenue": TARGETS["m3_useful_revenue_pct"],
            "verdict_global": _verdict(
                m3.global_pct, BASELINES["m3_useful_global_pct"], 50.0, higher_is_better=True
            ),
            "verdict_revenue": _verdict(
                m3.revenue_pct, None, TARGETS["m3_useful_revenue_pct"], higher_is_better=True
            ),
        },
        "m4": {
            "by_client": m4.by_client,
            "by_client_30d": m4.by_client_30d,
            "baseline_client_b": BASELINES["m4_client-b"],
            "baseline_client_a": BASELINES["m4_client-a"],
            "target_client_b": TARGETS["m4_client-b"],
        },
        "m5": {
            "signal_available": m5.signal_available,
            "note": m5.note,
            "revenue_blocks": m5.revenue_blocks,
            "total_blocks": m5.total_blocks,
        },
    }


def _bar(pct: float, width: int = 20) -> str:
    filled = round(pct / 100 * width)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {pct:5.1f}%"


def print_report(data: dict[str, Any]) -> None:
    m1 = data["m1"]
    m2 = data["m2"]
    m3 = data["m3"]
    m4 = data["m4"]
    m5 = data["m5"]

    W = 70
    sep = "─" * W

    print()
    print("=" * W)
    print(" ARIS4U — LEDGER MULTIPLICADOR REVENUE")
    print(
        f" Fecha: {data['run_date']}   Falsador: {data['falsador_date']}  ({data['days_to_falsador']}d)"
    )
    print("=" * W)

    # M1 — Auto-referencia
    print()
    print("M1  AUTO-REFERENCIA (decisions.client_id)")
    print(sep)
    print(f"    Total decisions : {m1['total_decisions']:,}")
    print()
    print("    SELF  (aris4u + lab-project-3 + sin-tag):")
    print(f"      Total  : {m1['self_total']:>5}  {_bar(m1['self_pct_total'])}")
    print(f"      30 d   : {m1['self_total']:>5}  {_bar(m1['self_pct_30d'])}")
    print(
        f"      Baseline {m1['baseline_self']}%  →  Target <{m1['target_self']}%   Veredicto: {m1['verdict_self']}"
    )
    print()
    print("    REVENUE (client-b · client-c · client-d · client-a · client-e):")
    print(f"      Total  : {m1['revenue_total']:>5}  {_bar(m1['revenue_pct_total'])}")
    print(f"      30 d   : {m1['revenue_total']:>5}  {_bar(m1['revenue_pct_30d'])}")
    print(
        f"      Baseline {m1['baseline_revenue']}%  →  Target ≥{m1['target_revenue']}%   Veredicto: {m1['verdict_revenue']}"
    )

    # M2 — Recall fill-rate
    print()
    print("M2  RECALL FILL-RATE (recall_events.client)")
    print(sep)
    fill = m2["fill_pct"]
    print(f"    Eventos totales : {m2['total']}   Con tag: {m2['tagged']}   Fill: {fill:.1f}%")
    print(
        f"      session_start : {m2['session_start_tagged']}/{m2['session_start_total']}  "
        f"({_pct(m2['session_start_tagged'], m2['session_start_total']):.1f}%)"
    )
    print(
        f"      user_prompt   : {m2['user_prompt_tagged']}/{m2['user_prompt_total']}  "
        f"({_pct(m2['user_prompt_tagged'], m2['user_prompt_total']):.1f}%)"
    )
    print(
        f"    Baseline {m2['baseline']}%  →  Target >{m2['target']}%   Veredicto: {m2['verdict']}"
    )

    # M3 — Recall útil
    print()
    print("M3  RECALL ÚTIL (recall_feedback)")
    print(sep)
    print(f"    Global   : {m3['useful']}/{m3['total']}  {_bar(m3['global_pct'])}")
    print(
        f"      Baseline {m3['baseline_global']}%  →  Target >50%   Veredicto: {m3['verdict_global']}"
    )
    print(f"    Revenue  : {m3['revenue_useful']}/{m3['revenue_total']}  {_bar(m3['revenue_pct'])}")
    print(
        f"      Baseline n/a   →  Target >{m3['target_revenue']}%   Veredicto: {m3['verdict_revenue']}"
    )

    # M4 — Decisiones por cliente revenue
    print()
    print("M4  DECISIONES POR CLIENTE REVENUE (decisions.client_id)")
    print(sep)
    print(f"    {'Cliente':<14} {'Total':>6}  {'30d':>5}  {'Baseline':>8}  {'Target':>8}")
    print(f"    {'-------':<14} {'-----':>6}  {'---':>5}  {'--------':>8}  {'------':>8}")
    baselines_m4 = {"client-b": 4, "client-a": 2}
    targets_m4 = {"client-b": "≥40"}
    for c in REVENUE_CLIENTS:
        tot = m4["by_client"].get(c, 0)
        t30 = m4["by_client_30d"].get(c, 0)
        bl = baselines_m4.get(c, "—")
        tgt = targets_m4.get(c, "—")
        print(f"    {c:<14} {tot:>6}  {t30:>5}  {str(bl):>8}  {str(tgt):>8}")

    # M5 — Guard-blocks revenue
    print()
    print("M5  GUARD-BLOCKS REVENUE (gate_results.session_ref)")
    print(sep)
    if m5["signal_available"]:
        pct_rev = _pct(m5["revenue_blocks"], m5["total_blocks"])
        print(f"    Revenue blocks: {m5['revenue_blocks']}/{m5['total_blocks']}  ({pct_rev:.1f}%)")
    else:
        print(f"    SIN SEÑAL: {m5['note']}")

    print()
    print("=" * W)
    print(" Veredictos: ↑ mejorando  →  estable  ↓ empeorando  ? sin baseline")
    print("=" * W)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="ARIS4U Revenue Ledger — read-only")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--db", type=Path, default=None, help="Path to sessions.db")
    args = parser.parse_args()

    data = build_report(db=args.db)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_report(data)


if __name__ == "__main__":
    main()
