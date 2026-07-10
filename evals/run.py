#!/usr/bin/env python3
"""Proof-of-value entry point — ARIS4U recall benchmark vs baseline.

Corre el arnés RAG recall (run_rag_recall.py), carga el baseline guardado y
reporta los deltas clave. Sirve como evidencia del valor del recall del
amplificador: un recall@1 alto con latencia estable prueba que la memoria
semántica funciona como multiplicador real.

Uso:
    .venv312/bin/python evals/run.py                    # reporte interactivo
    .venv312/bin/python evals/run.py --n 200 --json     # JSON (CI/tracking)
    .venv312/bin/python evals/run.py --update-baseline  # regenera baseline
    python -m evals                                     # idem (via __main__.py)

Requiere Ollama vivo (embedder). Sale con código 2 si no está disponible.
Sale con código 1 si content recall@1 (exact) < RECALL_FLOOR (regresión).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_EVALS = Path(__file__).resolve().parent
_BASELINE = _EVALS / "rag_recall_baseline.json"
_PYTHON = ROOT / ".venv312" / "bin" / "python3"
_RUNNER = _EVALS / "run_rag_recall.py"

# P2-A floor — content recall@1 en modo exact; baseline actual = 0.985
RECALL_FLOOR: float = 0.88


def _invoke_runner(n: int, words: int, k: int) -> dict[str, Any]:
    """Llama run_rag_recall.py --json y devuelve el dict parseado.

    Args:
        n: Items a muestrear.
        words: Palabras de la query parcial.
        k: Top-k a recuperar.

    Returns:
        Reporte parseado del runner.

    Raises:
        SystemExit: Si rc=2 (embedder caído / vec store no disponible).
        RuntimeError: Si el runner falla inesperadamente.
    """
    result = subprocess.run(
        [str(_PYTHON), str(_RUNNER),
         "--n", str(n), "--words", str(words), "--k", str(k), "--json"],
        capture_output=True,
        text=True,
        timeout=360,
        cwd=str(ROOT),
    )
    if result.returncode == 2:
        print(f"[SKIP] {result.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    if result.returncode != 0:
        raise RuntimeError(
            f"runner failed (rc={result.returncode}):\n"
            f"STDOUT: {result.stdout[:500]}\nSTDERR: {result.stderr[:500]}"
        )
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def _load_baseline() -> dict[str, Any] | None:
    """Carga el baseline JSON; None si no existe o está corrupto."""
    if not _BASELINE.exists():
        return None
    try:
        return json.loads(_BASELINE.read_text())  # type: ignore[return-value]
    except Exception:
        return None


def _find_mode(report: dict[str, Any], mode: str) -> dict[str, Any] | None:
    """Extrae el resultado de un modo ('exact'/'partial') del reporte."""
    return next((r for r in report.get("results", []) if r["mode"] == mode), None)


def _delta_str(now: float, then: float | None, unit: str = "") -> str:
    """Formatea el delta respecto al baseline con signo explícito."""
    if then is None:
        return ""
    d = now - then
    sign = "+" if d >= 0 else ""
    return f"  ({sign}{d:.4f}{unit} vs baseline)"


def _print_human(report: dict[str, Any], baseline: dict[str, Any] | None) -> int:
    """Imprime el reporte de proof-of-value en formato legible.

    Args:
        report: Reporte actual del runner.
        baseline: Baseline guardado (puede ser None).

    Returns:
        0 si recall@1 >= RECALL_FLOOR, 1 si hay regresión.
    """
    b_exact = _find_mode(baseline, "exact") if baseline else None
    b_partial = _find_mode(baseline, "partial") if baseline else None
    exact = _find_mode(report, "exact")
    partial = _find_mode(report, "partial")

    now_vecs: int | str = report.get("vectors_total", "?")
    base_vecs: int | str = baseline.get("vectors_total", "?") if baseline else "?"
    vec_note = ""
    if isinstance(now_vecs, int) and isinstance(base_vecs, int):
        growth = now_vecs - base_vecs
        pct = growth / max(base_vecs, 1) * 100
        vec_note = f"  (baseline={base_vecs}, +{growth} = +{pct:.1f}%)"

    print("=== ARIS4U proof-of-value — recall benchmark ===")
    print(f"vectors_total: {now_vecs}{vec_note}")
    print(
        f"sampled: {report.get('sampled', '?')} | "
        f"usable: {report.get('usable', '?')} | k={report.get('k', '?')}"
    )

    recall_1_exact: float | None = None
    for mode_result, b_mode, label in (
        (exact, b_exact, "exact"),
        (partial, b_partial, "partial"),
    ):
        if mode_result is None:
            continue
        rc = mode_result["recall_at_content"]
        rs = mode_result["recall_at_strict"]
        lat = mode_result["latency_ms"]
        b_rc = b_mode.get("recall_at_content", {}) if b_mode else {}
        b_lat = b_mode.get("latency_ms", {}) if b_mode else {}

        print(
            f"\n[{label}] evaluated={mode_result['evaluated']}  "
            f"MRR content={mode_result['mrr_content']}  strict={mode_result['mrr_strict']}"
        )
        print(
            f"  content @1={rc['@1']}{_delta_str(rc['@1'], b_rc.get('@1'))}  "
            f"@5={rc['@5']}  @10={rc['@10']}"
        )
        print(f"  strict  @1={rs['@1']}  @5={rs['@5']}  @10={rs['@10']}")
        print(
            f"  latency p50={lat['p50']}ms"
            f"{_delta_str(lat['p50'], b_lat.get('p50'), 'ms')}  "
            f"mean={lat['mean']}ms  p95={lat['p95']}ms"
        )
        if label == "exact":
            recall_1_exact = rc["@1"]

    print()
    if recall_1_exact is not None:
        verdict = "PASS" if recall_1_exact >= RECALL_FLOOR else "FAIL"
        print(
            f"VERDICT {verdict}  content recall@1 (exact) = {recall_1_exact:.4f}  "
            f"(floor={RECALL_FLOOR})"
        )
        if recall_1_exact < RECALL_FLOOR:
            print(
                f"  REGRESION: {recall_1_exact:.4f} < {RECALL_FLOOR}. "
                "Revisar vector store / embedder / dimensión."
            )
            return 1
    print(
        "\nNota: strict = hit exacto por (source,id); content = texto idéntico "
        "(descuenta duplicados). Gap strict<content = redundancia en observations."
    )
    return 0


def main() -> int:
    """Punto de entrada CLI del proof-of-value benchmark."""
    ap = argparse.ArgumentParser(
        description="ARIS4U evals — proof-of-value benchmark (recall vs baseline)"
    )
    ap.add_argument("--n", type=int, default=200, help="items a muestrear (default 200)")
    ap.add_argument("--words", type=int, default=14, help="palabras de la query parcial")
    ap.add_argument("--k", type=int, default=10, help="top-k a recuperar")
    ap.add_argument("--json", action="store_true", help="salida JSON (CI/tracking)")
    ap.add_argument(
        "--update-baseline",
        action="store_true",
        help="sobreescribe rag_recall_baseline.json con el resultado actual",
    )
    args = ap.parse_args()

    report = _invoke_runner(args.n, args.words, args.k)
    baseline = _load_baseline()

    if args.update_baseline:
        _BASELINE.write_text(json.dumps(report, indent=2) + "\n")
        print(f"[baseline] actualizado → {_BASELINE.name}  "
              f"({report.get('vectors_total', '?')} vectors)", file=sys.stderr)
        baseline = report  # muestra delta 0 (confirmación de escritura)

    if args.json:
        print(json.dumps({"report": report, "baseline": baseline}, indent=2))
        return 0

    return _print_human(report, baseline)


if __name__ == "__main__":
    sys.exit(main())
