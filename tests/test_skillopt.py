"""Tests del loop SkillOpt (engine.v16.skillopt) — el paso EDIT + validation-gate.

Prueban el GATE de forma DETERMINISTA, sin LLM: el "agente" es una función pura
y el "optimizador" es determinista. El SCORER es el linter de migraciones REAL
del repo (tools/migration_linter.py, exit 0/1) — así el ground truth es el mismo
verificador que usa producción, sin red ni DB externa (CI-friendly).

Invariante central que se verifica: una edición se acepta SOLO si mejora estricto
en el set de validación; nunca degrada el skill.
"""

from __future__ import annotations

from typing import Any, Optional

from engine.v16.skillopt import SkillOptLoop, migration_linter_scorer

# La "regla" que un buen skill debe contener para evitar el bug del linter.
RULE = "REGLA: nunca uses NOW()/CURRENT_TIMESTAMP en el predicado WHERE de un indice parcial."

# SQL que el agente emite segun tenga (o no) la regla en el skill.
BAD_SQL = "CREATE INDEX idx_recent ON events (id) WHERE created_at > NOW();\n"
GOOD_SQL = "CREATE TABLE users (id bigint PRIMARY KEY, email text NOT NULL);\n"


def _agent(skill_text: str, _task: Any) -> str:
    """Agente de juguete: si el skill trae la regla, emite SQL limpio; si no, el buggy."""
    del _task  # el agente no usa la tarea (firma Agent de 2 args)
    return GOOD_SQL if RULE in skill_text else BAD_SQL


def _fixing_optimizer(skill_text: str, failures: list[Any], rejected: list[str]) -> Optional[str]:
    """Optimizador determinista que SÍ arregla: añade la regla si falta."""
    del failures, rejected
    if RULE in skill_text:
        return None
    return (skill_text + "\n" + RULE).strip()


def _useless_optimizer(skill_text: str, failures: list[Any], rejected: list[str]) -> Optional[str]:
    """Optimizador que NO arregla: añade ruido sin la regla (debe ser rechazado)."""
    del failures, rejected
    return skill_text + "\n# comentario inutil que no cambia el comportamiento"


def _harmful_optimizer(skill_text: str, failures: list[Any], rejected: list[str]) -> Optional[str]:
    """Optimizador que DEGRADA: quita la regla (el gate debe rechazarlo)."""
    del failures, rejected
    return skill_text.replace(RULE, "").strip()


TASKS = list(range(10))  # 10 tareas (el agente/linter las ignoran; basta su numero)


# --- 0) El harness de verificacion es REAL ------------------------------------


def test_linter_scorer_is_real_ground_truth() -> None:
    assert migration_linter_scorer(GOOD_SQL) == 1.0
    assert migration_linter_scorer(BAD_SQL) == 0.0


# --- 1) Una edicion que MEJORA se acepta --------------------------------------


def test_improving_edit_is_accepted() -> None:
    loop = SkillOptLoop(_agent, migration_linter_scorer, _fixing_optimizer)
    result = loop.run("", TASKS, max_edits=5)
    assert result.base_score == 0.0          # skill vacio -> SQL buggy -> 0
    assert result.best_score == 1.0          # tras la edicion -> SQL limpio -> 1
    assert result.accepted_edits >= 1
    assert RULE in result.best_skill         # la regla quedo incorporada


# --- 2) Una edicion que NO mejora se rechaza y se recuerda ---------------------


def test_non_improving_edit_is_rejected_and_buffered() -> None:
    loop = SkillOptLoop(_agent, migration_linter_scorer, _useless_optimizer)
    result = loop.run("", TASKS, max_edits=5, patience=2)
    assert result.best_score == 0.0          # nunca mejoro
    assert result.accepted_edits == 0        # ninguna edicion aceptada
    assert len(loop.rejected) >= 1           # rejected-buffer poblado
    assert RULE not in result.best_skill


# --- 3) El gate NUNCA degrada un skill bueno ----------------------------------


def test_gate_never_degrades_good_skill() -> None:
    good_skill = RULE
    loop = SkillOptLoop(_agent, migration_linter_scorer, _harmful_optimizer)
    result = loop.run(good_skill, TASKS, max_edits=5, patience=2)
    assert result.base_score == 1.0          # ya era perfecto
    assert result.best_score == 1.0          # sigue perfecto: la degradacion se rechazo
    assert result.accepted_edits == 0
    assert RULE in result.best_skill          # la regla NO se perdio


# --- 4) reward_sink recibe las señales (integracion con soft_reward_loop) ------


def test_reward_sink_receives_signals() -> None:
    seen: list[tuple[str, float, str]] = []

    def sink(decision_id: str, reward: float, caller: str) -> None:
        seen.append((decision_id, reward, caller))

    loop = SkillOptLoop(_agent, migration_linter_scorer, _fixing_optimizer, reward_sink=sink)
    loop.run("", TASKS, max_edits=3)
    assert seen, "el reward_sink deberia recibir al menos una señal"
    assert all(c == "skillopt" for _, _, c in seen)
    assert any(did.endswith(":accepted") for did, _, _ in seen)


# --- 5) step() es la unidad: gate explicito -----------------------------------


def test_step_gate_logic() -> None:
    loop = SkillOptLoop(_agent, migration_linter_scorer, _fixing_optimizer)
    res = loop.step("", train_tasks=[1, 2, 3], val_tasks=[4, 5])
    assert res.accepted is True
    assert res.old_score == 0.0 and res.new_score == 1.0
    assert res.edit is not None and RULE in res.edit
