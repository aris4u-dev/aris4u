"""Capacity Advisor — asesor de capacidad DISCIPLINADO sobre queueing.py.

Núcleo bulletproof (decisión del usuario 2026-07-01, ver
architecture/BLUEPRINT_MOTOR_METODO_CROSS_DOMAIN.md §14): a-demanda, local-only,
exige datos MEDIDOS (n>=30 o REHÚSA), devuelve RANGO + supuestos + rehúso — nunca un
escalar con falsa autoridad. Chequea los supuestos del modelo (servicio exponencial,
llegadas Poisson) contra los DATOS, no contra fe.

Uso como librería::

    from engine.v16.orchestration import capacity_advisor as ca
    adv = ca.advise(service_times_seconds, servers=16)
    print(ca.format_report(adv))

Uso como CLI (desde la raíz del repo)::

    python -m engine.v16.orchestration.capacity_advisor --from-jsonl-dir DIR --servers 16
    python -m engine.v16.orchestration.capacity_advisor --from-file times.txt --arrival-rate-per-hour 6

Todos los tiempos en SEGUNDOS; las tasas se reportan por hora.
"""

from __future__ import annotations

import argparse
import glob
import math
import statistics as st
from dataclasses import dataclass
from collections.abc import Sequence

from . import queueing as q

_EXP_CV_LO, _EXP_CV_HI = 0.8, 1.2  # CV de servicio para aceptar M/M/s (exponencial)
_POISSON_CV_LO, _POISSON_CV_HI = 0.7, 1.4  # CV inter-arribo para aceptar Poisson


@dataclass(frozen=True)
class CapacityAdvice:
    """Resultado disciplinado del asesor. Si ``refused`` es True, solo ``reason`` vale."""

    refused: bool
    reason: str
    n: int
    mean_service_s: float
    cv_service: float
    ci95_service_s: tuple[float, float]
    cv_arrival: float | None
    exponential_ok: bool
    poisson_ok: bool | None
    question: str
    answer_low: float | None
    answer_high: float | None
    answer_unit: str
    pk_factor: float
    decision_grade: bool
    caveats: tuple[str, ...]


def _service_stats(
    service_times: Sequence[float],
) -> tuple[int, float, float, tuple[float, float]]:
    """Devuelve (n, media, CV, IC95%-de-la-media) de los tiempos de servicio."""
    n = len(service_times)
    mean = st.mean(service_times)
    sd = st.stdev(service_times) if n > 1 else 0.0
    cv = sd / mean if mean > 0 else float("nan")
    ci = 1.96 * sd / math.sqrt(n) if n > 1 else 0.0
    return n, mean, cv, (mean - ci, mean + ci)


def _arrival_cv(arrival_times_epoch: Sequence[float] | None) -> float | None:
    """CV de los tiempos inter-arribo (≈1 si Poisson, ≫1 si ráfaga); None si no hay datos."""
    if not arrival_times_epoch or len(arrival_times_epoch) < 3:
        return None
    ordered = sorted(arrival_times_epoch)
    # Tras sort todos los IAT son ≥0; se CONSERVAN los 0 (arribos simultáneos) a
    # propósito: son la señal de ráfaga/batch que queremos detectar (suben el CV).
    iats = [b - a for a, b in zip(ordered, ordered[1:])]
    if len(iats) < 2:
        return None
    mean = st.mean(iats)
    if mean <= 0:
        return None
    return st.stdev(iats) / mean


def _max_offered_load(servers: int, target_wait_prob: float) -> float:
    """Mayor carga ofrecida a (Erlangs) con P(espera)<=target para ``servers``."""
    a, step, best = 0.05, 0.05, 0.0
    while a < servers:
        if q.erlang_c(servers, a) <= target_wait_prob:
            best = a
            a += step
        else:
            break
    return best


def _min_servers(offered_load: float, target_wait_prob: float) -> int:
    """Menor número de servidores con P(espera)<=target para la carga dada."""
    s = int(offered_load) + 1
    ceiling = s + 200
    while s < ceiling:
        if q.erlang_c(s, offered_load) <= target_wait_prob:
            return s
        s += 1
    raise RuntimeError(
        f"sin nº de servidores válido bajo {ceiling} para carga {offered_load:.2f}"
    )


def _refusal(n: int, min_samples: int) -> CapacityAdvice:
    """Construye una respuesta de REHÚSO por datos insuficientes."""
    return CapacityAdvice(
        refused=True,
        reason=(
            f"n={n} < {min_samples} muestras. REHÚSO dar un número; "
            "mide más tiempos de servicio antes."
        ),
        n=n,
        mean_service_s=0.0,
        cv_service=float("nan"),
        ci95_service_s=(0.0, 0.0),
        cv_arrival=None,
        exponential_ok=False,
        poisson_ok=None,
        question="",
        answer_low=None,
        answer_high=None,
        answer_unit="",
        pk_factor=float("nan"),
        decision_grade=False,
        caveats=(),
    )


def _answer_question(
    servers: int | None,
    arrival_rate_per_hour: float | None,
    target_wait_prob: float,
    mu_lo: float,
    mu_hi: float,
) -> tuple[float, float, str, str]:
    """Devuelve (respuesta_baja, respuesta_alta, pregunta, unidad) para la pregunta pedida."""
    if servers is not None:
        if servers < 1:
            raise ValueError("servers debe ser >= 1")
        a_star = _max_offered_load(servers, target_wait_prob)
        if a_star <= 0.0:
            raise ValueError(
                f"objetivo P(espera)={target_wait_prob:.1%} inalcanzable con s={servers}"
            )
        question = f"¿Carga máxima sostenible con s={servers} a P(espera)<={target_wait_prob:.0%}?"
        return a_star * mu_lo, a_star * mu_hi, question, "llegadas/hora"
    assert arrival_rate_per_hour is not None  # garantizado por advise
    a_lo = arrival_rate_per_hour / mu_hi  # μ alta → carga baja
    a_hi = arrival_rate_per_hour / mu_lo
    question = (
        f"¿Mínimo de servidores para λ={arrival_rate_per_hour:.1f}/h "
        f"a P(espera)<={target_wait_prob:.0%}?"
    )
    return (
        float(_min_servers(a_lo, target_wait_prob)),
        float(_min_servers(a_hi, target_wait_prob)),
        question,
        "servidores",
    )


def _build_caveats(
    ci_degenerate: bool,
    cv: float,
    exp_ok: bool,
    pk: float,
    cv_arr: float | None,
    poisson_ok: bool | None,
) -> tuple[str, ...]:
    """Arma los supuestos rotos con la dirección de sesgo correcta (servicio y llegadas)."""
    caveats: list[str] = []
    if ci_degenerate:
        caveats.append(
            "IC95% de la media cruzó cero (varianza alta, n chico) → rango degenerado"
        )
    if not exp_ok:
        direction = "sobre" if cv < 1 else "sub"
        caveats.append(
            f"servicio NO exponencial (CV={cv:.2f}) → M/M/s {direction}-estima la espera "
            f"(factor P-K {pk:.2f}×); modelo honesto = M/G/s"
        )
    if poisson_ok is None:
        caveats.append("llegadas NO verificadas (sin timestamps) → Poisson SIN comprobar")
    elif cv_arr is not None and cv_arr > _POISSON_CV_HI:
        caveats.append(
            f"llegadas NO Poisson (CV_inter-arribo={cv_arr:.2f}, ráfaga) → M/M/s subestima"
        )
    elif not poisson_ok:
        caveats.append(
            f"llegadas NO Poisson (CV_inter-arribo={cv_arr:.2f}, más regular) → M/M/s sobreestima"
        )
    return tuple(caveats)


def advise(
    service_times: Sequence[float],
    *,
    servers: int | None = None,
    arrival_rate_per_hour: float | None = None,
    target_wait_prob: float = 0.10,
    arrival_times_epoch: Sequence[float] | None = None,
    min_samples: int = 30,
) -> CapacityAdvice:
    """Aconseja capacidad con disciplina bulletproof.

    Debe darse exactamente uno de ``servers`` (→ pregunta: máxima carga sostenible) o
    ``arrival_rate_per_hour`` (→ pregunta: mínimo de servidores).

    Args:
        service_times: Tiempos de servicio medidos, en segundos (n>=min_samples o REHÚSA).
        servers: Si se da, responde la carga máxima sostenible para ese nº de servidores.
        arrival_rate_per_hour: Si se da, responde el mínimo de servidores para esa tasa.
        target_wait_prob: Objetivo de P(espera) (Erlang C), en (0, 1). Default 0.10.
        arrival_times_epoch: Opcional; epoch-segundos de llegadas, para chequear Poisson.
        min_samples: Mínimo de muestras para no rehusar. Default 30.

    Returns:
        CapacityAdvice; ``refused``=True (con ``reason``) si faltan datos.

    Raises:
        ValueError: Si no se da exactamente una pregunta o los parámetros son inválidos.
    """
    if (servers is None) == (arrival_rate_per_hour is None):
        raise ValueError(
            "da exactamente uno: servers (máx carga) o arrival_rate_per_hour (mín servidores)"
        )
    if not 0.0 < target_wait_prob < 1.0:
        raise ValueError("target_wait_prob debe estar en (0, 1)")

    n = len(service_times)
    if n < min_samples:
        return _refusal(n, min_samples)

    n, mean_s, cv, (ci_lo, ci_hi) = _service_stats(service_times)
    if mean_s <= 0:
        raise ValueError("los tiempos de servicio deben ser > 0")
    ci_degenerate = ci_lo <= 0.0
    if ci_degenerate:
        ci_lo = mean_s * 0.01  # piso: el IC95% cruzó cero (varianza alta + n chico)
    cv_arr = _arrival_cv(arrival_times_epoch)

    exp_ok = _EXP_CV_LO <= cv <= _EXP_CV_HI
    poisson_ok: bool | None = (
        None if cv_arr is None else (_POISSON_CV_LO <= cv_arr <= _POISSON_CV_HI)
    )
    pk = (1.0 + cv * cv) / 2.0
    mu_lo, mu_hi = 3600.0 / ci_hi, 3600.0 / ci_lo  # servicio lento→μ menor

    answer_low, answer_high, question, unit = _answer_question(
        servers, arrival_rate_per_hour, target_wait_prob, mu_lo, mu_hi
    )
    caveats = _build_caveats(ci_degenerate, cv, exp_ok, pk, cv_arr, poisson_ok)

    return CapacityAdvice(
        refused=False,
        reason="",
        n=n,
        mean_service_s=mean_s,
        cv_service=cv,
        ci95_service_s=(ci_lo, ci_hi),
        cv_arrival=cv_arr,
        exponential_ok=exp_ok,
        poisson_ok=poisson_ok,
        question=question,
        answer_low=answer_low,
        answer_high=answer_high,
        answer_unit=unit,
        pk_factor=pk,
        decision_grade=exp_ok and poisson_ok is True,
        caveats=caveats,
    )


def format_report(adv: CapacityAdvice) -> str:
    """Reporte de texto disciplinado (medido / pregunta / respuesta / supuestos / veredicto)."""
    if adv.refused:
        return f"[REHÚSO] {adv.reason}"
    lo, hi = adv.ci95_service_s
    lines = [
        f"[MEDIDO] n={adv.n} · servicio media={adv.mean_service_s:.0f}s "
        f"(IC95% {lo:.0f}-{hi:.0f}s) · CV={adv.cv_service:.2f}",
        f"[PREGUNTA] {adv.question}",
        f"[RESPUESTA] {adv.answer_low:.1f}–{adv.answer_high:.1f} {adv.answer_unit}  (RANGO, no escalar)",
    ]
    if adv.caveats:
        lines.append("[SUPUESTOS ROTOS]")
        lines.extend(f"   - {c}" for c in adv.caveats)
    grade = "SÍ" if adv.decision_grade else "NO — boceto; verifica los supuestos rotos"
    lines.append(f"[VEREDICTO] grado-decisión={grade} · reproducible=sí (determinista)")
    return "\n".join(lines)


def service_data_from_jsonl_dir(directory: str) -> tuple[list[float], list[float]]:
    """Extrae (duraciones_s, inicios_epoch) de los ``agent-*.jsonl`` bajo ``directory``."""
    from ._transcript import agent_span

    durations: list[float] = []
    starts: list[float] = []
    for path in glob.glob(f"{directory}/**/agent-*.jsonl", recursive=True):
        span = agent_span(path)
        if span is not None:
            durations.append((span[1] - span[0]).total_seconds())
            starts.append(span[0].timestamp())
    return durations, starts


def _read_floats(path: str) -> list[float]:
    """Lee floats (uno por línea o separados por espacios) de un archivo."""
    with open(path, encoding="utf-8") as fh:
        return [float(x) for x in fh.read().split()]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI. Devuelve 0 si OK, 1 si rehúsa por datos insuficientes."""
    parser = argparse.ArgumentParser(
        description="Asesor de capacidad disciplinado (núcleo bulletproof)."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-jsonl-dir", help="extrae duraciones de agent-*.jsonl bajo el dir")
    src.add_argument("--from-file", help="tiempos de servicio en segundos (uno por línea)")
    ask = parser.add_mutually_exclusive_group(required=True)
    ask.add_argument("--servers", type=int, help="máxima carga sostenible para N servidores")
    ask.add_argument(
        "--arrival-rate-per-hour", type=float, help="mínimo de servidores para esa tasa"
    )
    parser.add_argument("--target", type=float, default=0.10, help="objetivo P(espera) (0-1)")
    parser.add_argument("--min-samples", type=int, default=30)
    args = parser.parse_args(argv)

    arrivals: list[float] | None = None
    if args.from_jsonl_dir:
        service, arrivals = service_data_from_jsonl_dir(args.from_jsonl_dir)
    else:
        service = _read_floats(args.from_file)

    adv = advise(
        service,
        servers=args.servers,
        arrival_rate_per_hour=args.arrival_rate_per_hour,
        target_wait_prob=args.target,
        arrival_times_epoch=arrivals,
        min_samples=args.min_samples,
    )
    print(format_report(adv))
    return 1 if adv.refused else 0


if __name__ == "__main__":
    raise SystemExit(main())
