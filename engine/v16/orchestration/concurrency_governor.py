"""Concurrency Governor — decide cuántos agentes lanzar en paralelo, SEGURO.

RAM-primero (el límite real del M5) + TIPO DE AGENTE + μ medido. Complementa al
guard bloqueante ``ram-saturation-guard`` (que evita SATURAR) diciendo además el
número seguro para no SUB-paralelizar. Es ADVISORY: informa la decisión de fan-out,
no la fuerza (los hooks no pueden forzar a Claude).

El nº seguro depende de qué carga cada agente localmente:
  - reasoning        (nube, ~0.35 GB c/u)          → suele topar en el harness (16)
  - shared-model     (1 modelo para todos, +5 GB)  → 16 tras cargar el modelo 1 vez
  - per-agent-model  (cada agente su 7B, ~5 GB)    → ~3 (RAM)
  - build-test       (pytest/npm, ~1.5 GB c/u)     → ~12 (RAM)

Uso CLI (desde la raíz del repo)::

    python -m engine.v16.orchestration.concurrency_governor
    python -m engine.v16.orchestration.concurrency_governor --subagents-dir DIR
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics as st
import subprocess
from dataclasses import dataclass
from collections.abc import Mapping

from ._transcript import agent_span

DEFAULT_LOG = os.path.expanduser("~/.claude/data/governor_durations.jsonl")

# perfil -> (gb_fijo_una_vez, gb_por_agente)
PROFILES: dict[str, tuple[float, float]] = {
    "reasoning": (0.0, 0.35),
    "shared-model": (5.0, 0.35),
    "per-agent-model": (0.0, 5.0),
    "build-test": (0.0, 1.5),
}
DEFAULT_MARGIN_GB = 8.0  # el guard bloquea si disponible < 8 GB
_SWAP_HOLD_MB = 100.0


@dataclass(frozen=True)
class GovernorDecision:
    """Decisión del gobernador. Si ``hold`` es True, no lanzar (todo safe=0)."""

    hold: bool
    reason: str
    avail_gb: float
    swap_mb: float
    harness_cap: int
    usable_gb: float
    safe_by_profile: Mapping[str, int]


def _safe_count(usable_gb: float, harness_cap: int, fixed_gb: float, per_gb: float) -> int:
    """Agentes seguros para un perfil: min(harness, RAM-usable / costo), o 0."""
    room = usable_gb - fixed_gb
    if room <= 0 or per_gb <= 0:
        return 0
    return min(harness_cap, int(room // per_gb))


def decide(
    avail_gb: float,
    swap_mb: float,
    cores: int,
    *,
    margin_gb: float = DEFAULT_MARGIN_GB,
    harness_cap: int | None = None,
) -> GovernorDecision:
    """Decide el nº seguro de agentes por perfil (función PURA, testeable sin I/O).

    Args:
        avail_gb: RAM disponible en vivo (GB).
        swap_mb: Swap en uso (MB); >100 ⇒ HOLD.
        cores: Núcleos de CPU (para el tope del harness min(16, cores-2)).
        margin_gb: Margen de seguridad de RAM a reservar. Default 8.
        harness_cap: Override del tope del harness (default min(16, cores-2)).

    Returns:
        GovernorDecision con ``safe_by_profile`` (0 en todos si ``hold``).
    """
    hc = harness_cap if harness_cap is not None else max(1, min(16, cores - 2))
    usable = max(0.0, avail_gb - margin_gb)
    if swap_mb > _SWAP_HOLD_MB or avail_gb < margin_gb:
        return GovernorDecision(
            hold=True,
            reason=f"swap={swap_mb:.0f}MB / disponible={avail_gb:.1f}GB tenso → HOLD",
            avail_gb=avail_gb,
            swap_mb=swap_mb,
            harness_cap=hc,
            usable_gb=usable,
            safe_by_profile={p: 0 for p in PROFILES},
        )
    safe = {p: _safe_count(usable, hc, fx, per) for p, (fx, per) in PROFILES.items()}
    return GovernorDecision(
        hold=False,
        reason="",
        avail_gb=avail_gb,
        swap_mb=swap_mb,
        harness_cap=hc,
        usable_gb=usable,
        safe_by_profile=safe,
    )


def _parse_ram_report(output: str) -> tuple[float, float] | None:
    """Extrae (GB disponibles, swap MB) del texto de ram-report; None si no matchea."""
    disp = re.search(r"DISPONIBLE.*?=\s*([\d.]+)\s*GB", output)
    swap = re.search(r"swap\s*([\d.]+)\s*M", output)
    if disp is None or swap is None:
        return None
    return float(disp.group(1)), float(swap.group(1))


def read_live_ram() -> tuple[float, float] | None:
    """Lee (GB disponibles, swap MB) del ram-report. None si la MEDICIÓN falla.

    Devolver None (no un sentinel -1) es deliberado: un fallo de medición NO debe
    disfrazarse de "RAM tensa / HOLD". El caller hace fail-open honesto.
    """
    try:
        out = subprocess.run(
            ["bash", os.path.expanduser("~/.claude/bin/ram-report.sh")],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_ram_report(out)


def measure_mu_seconds(subagents_dir: str) -> tuple[float, int]:
    """μ (tiempo de servicio medio) y n de agentes reales bajo ``subagents_dir``."""
    from . import capacity_advisor as ca  # lazy: evita cargar numpy en el path --oneline

    durations, _ = ca.service_data_from_jsonl_dir(subagents_dir)
    return (st.mean(durations), len(durations)) if durations else (0.0, 0)


def _seen_agent_ids(log_path: str) -> set[str]:
    """agent_ids ya presentes en el log persistente (para dedup)."""
    if not os.path.exists(log_path):
        return set()
    ids: set[str] = set()
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                ids.add(json.loads(line)["agent_id"])
            except (json.JSONDecodeError, KeyError):
                continue
    return ids


def _duration_of(path: str) -> float | None:
    """Duración (s) de un agent-*.jsonl; None si tiene < 2 timestamps."""
    span = agent_span(path)
    return (span[1] - span[0]).total_seconds() if span else None


def record_durations(
    scan_dir: str, log_path: str = DEFAULT_LOG, max_age_hours: float = 6.0
) -> int:
    """Anexa (APPEND-ONLY) duraciones de agentes NUEVOS a un log persistente.

    Escanea agent-*.jsonl recientes bajo ``scan_dir``, deduplica por agent_id contra
    el log, y anexa los no vistos. NUNCA modifica ni borra lo existente.

    Returns:
        Cuántas duraciones nuevas se anexaron.
    """
    import time  # local

    seen = _seen_agent_ids(log_path)
    cutoff = time.time() - max_age_hours * 3600.0
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    added = 0
    with open(log_path, "a", encoding="utf-8") as out:
        for path in glob.glob(f"{scan_dir}/**/agent-*.jsonl", recursive=True):
            agent_id = os.path.basename(path)[len("agent-") : -len(".jsonl")]
            if agent_id in seen:
                continue
            try:
                if os.path.getmtime(path) < cutoff:
                    continue
            except OSError:
                continue
            dur = _duration_of(path)
            if dur is None:
                continue
            out.write(json.dumps({"agent_id": agent_id, "duration_s": dur}) + "\n")
            seen.add(agent_id)
            added += 1
    return added


def read_recent_mu(log_path: str = DEFAULT_LOG, limit: int = 200) -> tuple[float, int]:
    """μ (media) y n de las últimas ``limit`` duraciones del log persistente."""
    if not os.path.exists(log_path):
        return 0.0, 0
    durs: list[float] = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                durs.append(float(json.loads(line)["duration_s"]))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    durs = durs[-limit:]
    return (st.mean(durs), len(durs)) if durs else (0.0, 0)


def format_decision(dec: GovernorDecision, mu_s: float = 0.0, n: int = 0) -> str:
    """Reporte de texto del gobernador (RAM viva, tabla por perfil, honestidad)."""
    head = (
        f"[EN VIVO] RAM disp {dec.avail_gb:.1f} GB · swap {dec.swap_mb:.0f} MB · "
        f"harness {dec.harness_cap} · usable {dec.usable_gb:.1f} GB"
    )
    if dec.hold:
        return f"{head}\n[DECISIÓN] ⛔ HOLD — {dec.reason}"
    lines = [head]
    if n:
        lines.append(f"[MEDIDO] μ reasoning ≈ {mu_s:.0f}s (~{mu_s / 60:.1f} min, {n} agentes)")
    lines.append("[SEGURO AHORA por tipo de agente]")
    for profile, safe in dec.safe_by_profile.items():
        binder = "harness" if safe == dec.harness_cap else ("RAM" if safe > 0 else "sin RAM")
        lines.append(f"   {profile:<18} → {safe:>2}  ({binder})")
    lines.append(
        "[SUPUESTO] costos GB/agente ASUMIDOS (reasoning 0.35 · modelo 5 · build 1.5), no medidos"
    )
    return "\n".join(lines)


def format_oneline(dec: GovernorDecision, mu_s: float = 0.0) -> str:
    """Una sola línea compacta (para inyectar en contexto vía hook)."""
    if dec.hold:
        return (
            f"🚦 gobernador: ⛔ HOLD — RAM tensa "
            f"(swap {dec.swap_mb:.0f}MB / disp {dec.avail_gb:.1f}GB); no lances fan-out"
        )
    s = dec.safe_by_profile
    est = f" · tanda ~{mu_s / 60:.0f}min" if mu_s > 0 else ""
    return (
        f"🚦 gobernador (RAM {dec.avail_gb:.0f}GB, swap {dec.swap_mb:.0f}): concurrencia segura "
        f"= reasoning {s['reasoning']} · shared-model {s['shared-model']} · "
        f"per-agent-model {s['per-agent-model']} · build-test {s['build-test']}{est} "
        f"— dimensiona el fan-out por tipo"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: lee RAM viva, decide, imprime la tabla (o --oneline). 1 si HOLD."""
    parser = argparse.ArgumentParser(description="Gobernador de concurrencia (advisory).")
    parser.add_argument("--subagents-dir", help="dir con agent-*.jsonl para medir μ (opcional)")
    parser.add_argument("--record-durations", help="escanea el dir y anexa duraciones al log")
    parser.add_argument("--durations-log", default=DEFAULT_LOG, help="ruta del log de duraciones")
    parser.add_argument("--margin-gb", type=float, default=DEFAULT_MARGIN_GB)
    parser.add_argument("--oneline", action="store_true", help="salida compacta de 1 línea")
    args = parser.parse_args(argv)

    if args.record_durations:
        added = record_durations(args.record_durations, args.durations_log)
        print(f"registradas {added} duraciones nuevas → {args.durations_log}")
        return 0

    ram = read_live_ram()
    if ram is None:  # medición falló → FAIL-OPEN honesto: nunca un HOLD mentiroso
        if not args.oneline:
            print("[gobernador] medición de RAM no disponible — sin veredicto")
        return 0
    dec = decide(ram[0], ram[1], os.cpu_count() or 18, margin_gb=args.margin_gb)
    if args.oneline:
        mu_s, _ = read_recent_mu(args.durations_log)
        print(format_oneline(dec, mu_s))
        return 1 if dec.hold else 0
    mu_s, n = measure_mu_seconds(args.subagents_dir) if args.subagents_dir else (0.0, 0)
    print(format_decision(dec, mu_s, n))
    return 1 if dec.hold else 0


if __name__ == "__main__":
    raise SystemExit(main())
