"""Scorer BLOQUEADO para `recall_usefulness.judge` — primer scorer sellado (Karpathy).

Adapta `recall_usefulness.judge(injected, query, response) -> (useful, score, matched)`
al contrato `Scorer = (artifact, task) -> float` de SkillOpt y lo SELLA con `LockedScorer`,
de modo que la DEFINICIÓN de "recall útil" (umbrales + cap de normalización + el propio
código de `judge`) quede fuera del alcance del agente que optimiza.

Por qué `recall_usefulness` es el primer sellado: es la métrica que la consola ya muestra
viva (Recall útil 158/281 = 56%) y su umbral de freeze (≥3 útiles/semana × 2 semanas) ES
"el número honesto" que el agente no debe poder tocar.

FRONTERA (política "empieza-solo-PR"): este módulo NO modifica `recall_usefulness.py`, NO
escribe en `data/sessions.db`, NI cambia el comportamiento del recall en runtime. Solo provee
el scorer sellado + el sello. Cablearlo al lazo real (persistir el sello con locked=1 y usarlo
para optimizar) es una decisión de PR humana.
"""

from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.v16.locked_scorer import LockedScorer, ScoreSeal  # noqa: E402
from tools.recall_usefulness import (  # noqa: E402
    MIN_DISTINCTIVE_LEN,
    MIN_TERMS,
    judge,
)

# Normalización: el score crudo de judge es len(distintivos)+2·len(ids), no acotado.
# `score_cap` lo mapea a [0,1] (cap = "saturación" de utilidad). Parte de la métrica sellada.
SCORE_CAP = 6.0

# La DEFINICIÓN de la métrica que se sella (lo que un humano fija y el agente no toca).
# Incluye el hash del código de `judge` → si alguien altera su lógica, el sello no cuadra.
METRIC_DEF: dict[str, Any] = {
    "score_cap": SCORE_CAP,
    "min_terms": MIN_TERMS,
    "min_distinctive_len": MIN_DISTINCTIVE_LEN,
    "judge_src_sha": hashlib.sha256(inspect.getsource(judge).encode("utf-8")).hexdigest(),
}


def recall_useful_scorer(
    artifact: Any,
    task: Any,
    *,
    score_cap: float = SCORE_CAP,
    min_terms: int = MIN_TERMS,
    min_distinctive_len: int = MIN_DISTINCTIVE_LEN,
    judge_src_sha: str = "",
) -> float:
    """Scorer en [0,1]. `artifact` = texto de respuesta de Claude; `task` = dict con
    `injected` (bloque RECALL) y `query` (prompt). Los kwargs son la métrica sellada.

    `min_terms`/`min_distinctive_len`/`judge_src_sha` se sellan (no alteran el cálculo de
    `judge`, que usa sus propias constantes); su rol es que el SELLO detecte si la definición
    cambió. `score_cap` sí normaliza.
    """
    del min_terms, min_distinctive_len, judge_src_sha  # sellados, no recalculados aquí
    injected = task.get("injected", []) if isinstance(task, dict) else []
    query = task.get("query", "") if isinstance(task, dict) else ""
    _useful, raw, _matched = judge(injected, query, str(artifact))
    if score_cap <= 0:
        raise ValueError("score_cap debe ser > 0")
    return max(0.0, min(float(raw), score_cap)) / score_cap


def make_locked_recall_scorer(seal: ScoreSeal | None = None) -> LockedScorer:
    """Construye el scorer sellado. Sin `seal` = modo sello (humano firma); con `seal` =
    modo verificación (aborta si la definición fue alterada)."""
    return LockedScorer(
        recall_useful_scorer,
        dict(METRIC_DEF),
        metric_id="recall_usefulness_v1",
        seal=seal,
    )


if __name__ == "__main__":
    s = make_locked_recall_scorer()
    print(f"[sello] {s.seal.metric_id} digest={s.seal.digest[:12]} cap={SCORE_CAP}")
    # Demostración: una respuesta que USA un identificador novedoso del recall = útil → score alto.
    task = {"injected": ["usa la función soft_reward_loop.record_reward para el lazo"],
            "query": "como cierro el lazo de reward"}
    good = s("hay que llamar a soft_reward_loop.record_reward al final", task)
    bad = s("no estoy seguro", task)
    print(f"score(respuesta-que-usa-el-recall) = {good:.3f}")
    print(f"score(respuesta-que-lo-ignora)     = {bad:.3f}")
    print("OK: scorer sellado en [0,1]" if 0.0 <= bad <= good <= 1.0 else "FALLO")
