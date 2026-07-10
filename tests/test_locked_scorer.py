"""Tests del LockedScorer (engine.v16.locked_scorer) — el "scoring file bloqueado".

Verifican el invariante Karpathy: la DEFINICIÓN de la métrica (código + config) queda
sellada; cualquier alteración → MetricTamperError en vez de un número manipulado. Sin
LLM, sin red, sin DB (CI-friendly). Incluye la integración con recall_usefulness sellado.
"""

from __future__ import annotations

import pytest

from engine.v16.locked_scorer import LockedScorer, MetricTamperError, ScoreSeal
from engine.v16.skillopt import SkillOptLoop


def _scorer(artifact, _task, *, threshold: int = 3) -> float:
    """Scorer de juguete: 1.0 si len(artifact) >= threshold, si no 0.0."""
    return 1.0 if len(str(artifact)) >= threshold else 0.0


# --- Mecánica del sello -----------------------------------------------------------------


def test_seal_roundtrip_ok() -> None:
    """Sellar y reconstruir con el mismo (código, config) verifica sin error."""
    locked = LockedScorer(_scorer, {"threshold": 3}, metric_id="toy")
    seal = locked.seal
    again = LockedScorer(_scorer, {"threshold": 3}, metric_id="toy", seal=seal)
    assert again("abcd", None) == 1.0
    assert again("ab", None) == 0.0


def test_seal_is_deterministic() -> None:
    """El digest no depende de la sesión: mismo input → mismo digest."""
    a = LockedScorer(_scorer, {"threshold": 3}, metric_id="toy").seal.digest
    b = LockedScorer(_scorer, {"threshold": 3}, metric_id="toy").seal.digest
    assert a == b


def test_tamper_config_runtime_detected() -> None:
    """Mutar la config sellada en runtime dispara MetricTamperError al puntuar."""
    locked = LockedScorer(_scorer, {"threshold": 3}, metric_id="toy")
    locked._config["threshold"] = -1  # gaming: relajar el umbral
    with pytest.raises(MetricTamperError):
        locked("x", None)


def test_tamper_config_vs_seal_detected() -> None:
    """Reconstruir con config distinta a la sellada (cross-proceso) → MetricTamperError."""
    seal = LockedScorer(_scorer, {"threshold": 3}, metric_id="toy").seal
    with pytest.raises(MetricTamperError):
        LockedScorer(_scorer, {"threshold": 99}, metric_id="toy", seal=seal)


def test_unlocked_seal_rejected() -> None:
    """Un sello con locked=False se rechaza."""
    seal = LockedScorer(_scorer, {"threshold": 3}, metric_id="toy").seal
    unlocked = ScoreSeal(seal.metric_id, seal.digest, seal.config, locked=False)
    with pytest.raises(MetricTamperError):
        LockedScorer(_scorer, {"threshold": 3}, metric_id="toy", seal=unlocked)


def test_score_out_of_range_raises() -> None:
    """Un base scorer que devuelve fuera de [0,1] es un error, no un número silencioso."""
    locked = LockedScorer(lambda a, t: 5.0, {}, metric_id="bad")
    with pytest.raises(ValueError):
        locked("x", None)


def test_seal_json_roundtrip() -> None:
    """El manifiesto serializa y deserializa idéntico (lo que un humano commitea)."""
    seal = LockedScorer(_scorer, {"threshold": 3}, metric_id="toy").seal
    restored = ScoreSeal.from_json(seal.to_json())
    assert restored.digest == seal.digest
    assert restored.config == seal.config
    assert restored.locked is True


# --- Integración con SkillOptLoop -------------------------------------------------------


def test_plugs_into_skillopt_loop() -> None:
    """El scorer sellado entra como Scorer de SkillOptLoop y el gate nunca degrada."""
    locked = LockedScorer(_scorer, {"threshold": 5}, metric_id="toy")

    def optimizer(text: str, _flags: object, _rejected: object) -> str:
        return text + "x"  # añade longitud → sube el score

    loop = SkillOptLoop(agent=lambda s, t: s, scorer=locked, optimizer=optimizer)
    res = loop.run("ab", ["t1", "t2", "t3"], val_split=0.34, max_edits=10, patience=3)
    assert res.best_score >= res.base_score  # invariante del gate


# --- Integración con recall_usefulness sellado ------------------------------------------


def test_recall_locked_scorer_in_range() -> None:
    """El scorer sellado de recall_usefulness devuelve [0,1]; usar el recall > ignorarlo."""
    from tools.recall_usefulness_locked import make_locked_recall_scorer

    s = make_locked_recall_scorer()
    task = {"injected": ["usa soft_reward_loop.record_reward para el lazo"],
            "query": "como cierro el lazo"}
    used = s("llamar a soft_reward_loop.record_reward al final", task)
    ignored = s("no estoy seguro", task)
    assert 0.0 <= ignored <= used <= 1.0
    assert used > ignored


def test_recall_metric_def_tamper_detected() -> None:
    """Alterar la definición sellada de recall (p.ej. score_cap) vs el sello → error."""
    from tools.recall_usefulness_locked import make_locked_recall_scorer
    from engine.v16.locked_scorer import LockedScorer as LS
    from tools.recall_usefulness_locked import METRIC_DEF, recall_useful_scorer

    seal = make_locked_recall_scorer().seal
    tampered = dict(METRIC_DEF, score_cap=1.0)  # gaming: bajar el cap infla scores
    with pytest.raises(MetricTamperError):
        LS(recall_useful_scorer, tampered, metric_id="recall_usefulness_v1", seal=seal)
