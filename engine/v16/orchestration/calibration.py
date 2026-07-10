"""Calibración del estimador local — el gate que valida el sensor (§8.5 del blueprint).

⚠️ NO CABLEADO A DECISIONES VIVAS. Biblioteca matemática pura (regresión logística por
IRLS). Su razón de ser: comprobar empíricamente si el `promise_score` del cuerpo local
PREDICE el éxito real de Claude. Si no (odds-ratio ≈ 1, p > 0.05), el sensor es teatro y
NO debe alimentar la capa de decisión (§8.4).

✅ 2026-06-20: el `promise_score` YA se EMITE por llamada (dispatch_mlx lo deriva de los
logprobs = exp(logprob medio); aris_structure/aris_critique lo registran en la telemetría).
El disparador vive en `tools/f1_roi.run_calibration`: a las 30 llamadas etiquetadas CON
score, corre `sensor_is_predictive` sobre los pares (promise_score, útil) REALES y dictamina.
Falta solo acumular las 30 (uso real). Antes (2026-06-19): el score "no existía (F1 sin construir)".

Mapeo a ARIS4U:
    - logistic_fit          → regresión logística genérica (éxito ~ features).
    - sensor_is_predictive  → el gate binario: ¿confío en el promise_score del local?
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats

__all__ = [
    "LogisticFit",
    "logistic_fit",
    "auc_score",
    "SensorVerdict",
    "sensor_is_predictive",
]


@dataclass(frozen=True)
class LogisticFit:
    """Resultado de una regresión logística.

    Attributes:
        coef: Coeficientes β (incluye el intercepto en la posición 0).
        std_err: Errores estándar de cada coeficiente (de la inversa de la información).
        odds_ratios: exp(β) para cada coeficiente.
        p_values: p-valores de Wald (dos colas) por coeficiente.
        n: Número de observaciones.
        converged: True si el IRLS convergió.
    """

    coef: np.ndarray
    std_err: np.ndarray
    odds_ratios: np.ndarray
    p_values: np.ndarray
    n: int
    converged: bool


def _sigmoid(z: np.ndarray) -> np.ndarray:
    """Sigmoide numéricamente estable."""
    out = np.empty_like(z, dtype=float)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return out


def _stable_inverse(m: np.ndarray) -> np.ndarray:
    """Inversa numéricamente estable de una matriz SPD (información de Fisher).

    Usa `solve` por LU (consistente con el paso de Newton de logistic_fit) en vez de
    `inv` explícita — más estable. Si la matriz es singular/mal condicionada (p. ej.
    features correlacionados), cae a la pseudoinversa SVD (`pinv`), que degrada con
    gracia en vez de fallar. Hallazgo del propio aris_critique sobre este módulo.
    """
    k = m.shape[0]
    try:
        return np.linalg.solve(m, np.eye(k))
    except np.linalg.LinAlgError:
        return np.linalg.pinv(m)


def logistic_fit(
    x: np.ndarray, y: np.ndarray, *, max_iter: int = 100, tol: float = 1e-8
) -> LogisticFit:
    """Regresión logística binaria por mínimos cuadrados reponderados iterativos (IRLS).

    Añade automáticamente la columna de intercepto. Los errores estándar salen de la
    inversa de la matriz de información de Fisher (Xᵀ W X).

    Args:
        x: Matriz de features (n×p) o vector (n,) para un solo feature.
        y: Vector binario de resultados (n,), valores en {0, 1}.
        max_iter: Iteraciones máximas de IRLS.
        tol: Tolerancia de convergencia (norma del cambio de β).

    Returns:
        LogisticFit con coeficientes, errores, odds-ratios y p-valores.

    Raises:
        ValueError: Si dimensiones no coinciden, y no es binario, o la información es
            singular (p. ej. separación perfecta o feature constante).
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.ndim == 1:
        x = x.reshape(-1, 1)
    if x.shape[0] != y.shape[0]:
        raise ValueError("x e y deben tener el mismo número de filas")
    if not np.all(np.isin(y, (0.0, 1.0))):
        raise ValueError("y debe ser binario (0/1)")
    if len(np.unique(y)) < 2:
        raise ValueError("y debe contener ambas clases (0 y 1)")

    n = x.shape[0]
    design = np.column_stack([np.ones(n), x])
    beta = np.zeros(design.shape[1])
    converged = False
    for _ in range(max_iter):
        eta = design @ beta
        mu = _sigmoid(eta)
        w = np.clip(mu * (1.0 - mu), 1e-12, None)
        hessian = design.T @ (w[:, None] * design)
        grad = design.T @ (y - mu)
        try:
            step = np.linalg.solve(hessian, grad)
        except np.linalg.LinAlgError as exc:
            raise ValueError(
                "información singular (separación perfecta o feature constante)"
            ) from exc
        beta = beta + step
        if np.linalg.norm(step) < tol:
            converged = True
            break

    eta = design @ beta
    mu = _sigmoid(eta)
    w = np.clip(mu * (1.0 - mu), 1e-12, None)
    # Covarianza vía solve+pinv (estable), no inv explícita — consistente con el paso
    # de Newton de arriba. Hallazgo de aris_critique sobre este mismo módulo (2026-06-19).
    cov = _stable_inverse(design.T @ (w[:, None] * design))
    std_err = np.sqrt(np.diag(cov))
    z = beta / std_err
    p_values = 2.0 * (1.0 - stats.norm.cdf(np.abs(z)))
    return LogisticFit(
        coef=beta, std_err=std_err, odds_ratios=np.exp(beta),
        p_values=p_values, n=n, converged=converged,
    )


def auc_score(scores: np.ndarray, labels: np.ndarray) -> float:
    """Área bajo la curva ROC (AUC) vía el estadístico de Mann-Whitney.

    Args:
        scores: Puntuaciones continuas (mayor = más probable clase positiva).
        labels: Etiquetas binarias {0, 1}.

    Returns:
        AUC ∈ [0, 1]. 0.5 = sin poder discriminante.

    Raises:
        ValueError: Si no hay ambas clases.
    """
    scores = np.asarray(scores, dtype=float)
    labels = np.asarray(labels, dtype=float)
    pos = scores[labels == 1]
    neg = scores[labels == 0]
    if pos.size == 0 or neg.size == 0:
        raise ValueError("se requieren ambas clases para AUC")
    ranks = stats.rankdata(np.concatenate([pos, neg]))
    rank_pos = ranks[: pos.size].sum()
    auc = (rank_pos - pos.size * (pos.size + 1) / 2.0) / (pos.size * neg.size)
    return float(auc)


@dataclass(frozen=True)
class SensorVerdict:
    """Veredicto sobre si el sensor local predice el éxito real.

    Attributes:
        predictive: True si el score predice (p < alpha, AUC > 0.5, n suficiente).
        odds_ratio: exp(β) del score (cuánto sube la odds de éxito por unidad de score).
        p_value: p-valor de Wald del coeficiente del score.
        auc: Área bajo la curva ROC del score.
        n: Número de muestras usadas.
        reason: Explicación legible del veredicto.
    """

    predictive: bool
    odds_ratio: float
    p_value: float
    auc: float
    n: int
    reason: str


def sensor_is_predictive(
    scores: np.ndarray,
    successes: np.ndarray,
    *,
    min_samples: int = 30,
    alpha: float = 0.05,
) -> SensorVerdict:
    """Gate de §8.5: ¿el promise_score del local predice el éxito real de Claude?

    Ajusta una regresión logística éxito ~ score y dictamina. Si no es predictivo, el
    cuerpo local debe degradarse a estructurador de I/O y NO alimentar la capa de decisión.

    Args:
        scores: promise_scores del local (n,).
        successes: resultado real binario (n,), 1 = la rama/recall fue útil.
        min_samples: Mínimo de muestras para emitir un veredicto fiable.
        alpha: Nivel de significancia para el p-valor de Wald.

    Returns:
        SensorVerdict. predictive=False con reason explícita si los datos son insuficientes
        o el ajuste no es posible (nunca lanza por falta de datos: falla-cerrado a "no confío").
    """
    scores = np.asarray(scores, dtype=float)
    successes = np.asarray(successes, dtype=float)
    n = scores.shape[0]
    if n < min_samples:
        return SensorVerdict(
            predictive=False, odds_ratio=float("nan"), p_value=float("nan"),
            auc=float("nan"), n=n,
            reason=f"insuficientes muestras ({n} < {min_samples}); inconcluso",
        )
    try:
        fit = logistic_fit(scores, successes)
        auc = auc_score(scores, successes)
    except ValueError as exc:
        return SensorVerdict(
            predictive=False, odds_ratio=float("nan"), p_value=float("nan"),
            auc=float("nan"), n=n, reason=f"ajuste imposible: {exc}",
        )
    # coef[0] = intercepto, coef[1] = score
    p_score = float(fit.p_values[1])
    or_score = float(fit.odds_ratios[1])
    predictive = bool(p_score < alpha and auc > 0.5)
    if predictive:
        reason = f"predictivo: OR={or_score:.2f}, p={p_score:.4f}, AUC={auc:.3f}"
    else:
        reason = (
            f"NO predictivo (sensor = teatro): OR={or_score:.2f}, "
            f"p={p_score:.4f}, AUC={auc:.3f}"
        )
    return SensorVerdict(
        predictive=predictive, odds_ratio=or_score, p_value=p_score,
        auc=auc, n=n, reason=reason,
    )
