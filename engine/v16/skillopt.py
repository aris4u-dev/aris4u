"""SkillOpt — el paso EDIT + validation-gate del LEGO Learning Engine (el 20% que faltaba).

Propone una edición ACOTADA al texto de un skill y la ACEPTA solo si mejora
estrictamente el score agregado en un conjunto de tareas held-out. Las ediciones
rechazadas van a un buffer con su razón, para que el optimizador no las repita.
Espeja a Microsoft SkillOpt (arXiv:2605.23904): rollout -> reflect -> edit ->
validation-gate, con rejected-edit buffer.

Construido ENCIMA del motor existente, no lo duplica:
  - El SCORER es cualquier verificador determinista. El adaptador por defecto
    (`migration_linter_scorer`) usa `tools/migration_linter.py` (exit 0/1), que
    ya es el "ground truth verificable" del repo (sin LLM, sin red, sin DB).
  - La persistencia reusa `soft_reward_loop.record_reward` vía un `reward_sink`
    opcional (inyectado, no acoplado — así el test corre puro).
  - El OPTIMIZER es enchufable: Claude/Opus (o MLX como pre-filtro) en producción;
    una función determinista en los tests, para verificar el GATE sin LLM.

Política deliberada (resumida en architecture/PARKING.md §Marcos diferidos; detalle en git):
"empieza-solo-PR" — el loop PROPONE una edición validada; el merge a un skill real
lo decide un humano. Aquí devolvemos el texto aceptado + el historial; no escribimos
ningún SKILL.md en disco salvo que el caller lo pida explícitamente.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from collections.abc import Callable

# --- Contratos (todo enchufable, sin acoplar a un LLM concreto) -----------------

# Ejecuta el skill sobre una tarea y produce un artefacto (p.ej. SQL de migración).
Agent = Callable[[str, Any], Any]
# Puntúa el artefacto contra la tarea, en [0.0, 1.0] (1.0 = perfecto).
Scorer = Callable[[Any, Any], float]
# Propone un texto de skill nuevo a partir del actual, los fallos y el rejected-buffer.
# Devuelve None o el mismo texto si no propone cambio.
Optimizer = Callable[[str, list[Any], list[str]], Optional[str]]
# (Opcional) Reduce fallos a "flags" baratos antes de pasar al optimizer.
Reflector = Callable[[str, list[Any]], list[Any]]
# (Opcional) Sumidero de reward — en prod: soft_reward_loop.record_reward(id, r, caller).
# Retorno Any: el valor se ignora (record_reward devuelve bool; otros sinks, None).
RewardSink = Callable[[str, float, str], Any]


@dataclass
class StepResult:
    """Resultado de un paso del loop (una propuesta de edición evaluada)."""

    accepted: bool
    old_score: float
    new_score: float
    edit: Optional[str]  # texto del skill aceptado (None si se rechazó)
    reason: str


@dataclass
class RunResult:
    """Resultado de correr el loop completo."""

    best_skill: str
    best_score: float
    base_score: float
    accepted_edits: int
    rejected_edits: int
    history: list[StepResult] = field(default_factory=list)


def _digest(text: str, reason: str) -> str:
    """Huella corta de una edición rechazada (para el rejected-buffer)."""
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{h}:{reason}"


class SkillOptLoop:
    """El loop de optimización con validation-gate estricto.

    El invariante central (lo que lo hace seguro): una edición candidata solo
    se acepta si su score en el set de VALIDACIÓN supera estrictamente al actual.
    Sin esa mejora, se rechaza y se recuerda — nunca degrada el skill.
    """

    def __init__(
        self,
        agent: Agent,
        scorer: Scorer,
        optimizer: Optimizer,
        *,
        reflector: Optional[Reflector] = None,
        min_delta: float = 1e-9,
        reward_sink: Optional[RewardSink] = None,
        skill_id: str = "skillopt",
    ) -> None:
        self.agent = agent
        self.scorer = scorer
        self.optimizer = optimizer
        self.reflector = reflector
        self.min_delta = min_delta
        self.reward_sink = reward_sink
        self.skill_id = skill_id
        self.rejected: list[str] = []  # rejected-edit buffer (huella + razón)

    def evaluate(self, skill_text: str, tasks: list[Any]) -> float:
        """Score medio del skill sobre un conjunto de tareas."""
        if not tasks:
            return 0.0
        total = 0.0
        for task in tasks:
            total += self.scorer(self.agent(skill_text, task), task)
        return total / len(tasks)

    def _emit_reward(self, reward: float, accepted: bool) -> None:
        if self.reward_sink is not None:
            tag = "accepted" if accepted else "rejected"
            self.reward_sink(f"{self.skill_id}:{tag}", reward, "skillopt")

    def step(
        self, skill_text: str, train_tasks: list[Any], val_tasks: list[Any]
    ) -> StepResult:
        """Un paso: rollout(train) -> reflect -> edit -> validation-gate(val)."""
        base_val = self.evaluate(skill_text, val_tasks)

        # ROLLOUT: tareas de entrenamiento donde el skill aún no es perfecto.
        failures = [t for t in train_tasks if self.scorer(self.agent(skill_text, t), t) < 1.0]

        # REFLECT (opcional): comprime fallos a flags baratos.
        flags: list[Any] = self.reflector(skill_text, failures) if self.reflector else failures

        # EDIT: el optimizador propone, conociendo lo ya rechazado (no lo repite).
        candidate = self.optimizer(skill_text, flags, list(self.rejected))
        if candidate is None or candidate == skill_text:
            self._emit_reward(base_val, accepted=False)
            return StepResult(False, base_val, base_val, None, "optimizer no propuso cambio")

        # VALIDATION-GATE: aceptar SOLO si mejora estricto en held-out.
        cand_val = self.evaluate(candidate, val_tasks)
        if cand_val > base_val + self.min_delta:
            self._emit_reward(cand_val, accepted=True)
            return StepResult(
                True, base_val, cand_val, candidate, f"val {base_val:.3f} -> {cand_val:.3f}"
            )

        # RECHAZO: recordar la huella + razón para no reproponerla.
        self.rejected.append(_digest(candidate, f"val {cand_val:.3f} !> {base_val:.3f}"))
        self._emit_reward(base_val, accepted=False)
        return StepResult(
            False, base_val, cand_val, None, f"rechazada: val {cand_val:.3f} no supera {base_val:.3f}"
        )

    def run(
        self,
        skill_text: str,
        tasks: list[Any],
        *,
        val_split: float = 0.4,
        max_edits: int = 10,
        patience: int = 2,
    ) -> RunResult:
        """Corre el loop: split determinista train/val, edita hasta converger.

        Para tras `patience` rechazos consecutivos (señal de "seco", como SkillOpt).
        Nunca devuelve un skill peor que el de partida (el gate lo garantiza).
        """
        if not tasks:
            raise ValueError("se requieren tareas para optimizar")
        n_val = max(1, int(round(len(tasks) * val_split)))
        # Split determinista (sin azar — reproducible): val = cola, train = cabeza.
        val_tasks = tasks[-n_val:]
        train_tasks = tasks[:-n_val] or tasks  # si todo es val, train = todo

        current = skill_text
        base = self.evaluate(current, val_tasks)
        best_score = base
        history: list[StepResult] = []
        accepted = 0
        consecutive_rejects = 0

        for _ in range(max_edits):
            res = self.step(current, train_tasks, val_tasks)
            history.append(res)
            if res.accepted and res.edit is not None:
                current = res.edit
                best_score = res.new_score
                accepted += 1
                consecutive_rejects = 0
                if best_score >= 1.0 - self.min_delta:
                    break  # perfecto en val: no hay más que ganar
            else:
                consecutive_rejects += 1
                if consecutive_rejects >= patience:
                    break

        return RunResult(
            best_skill=current,
            best_score=best_score,
            base_score=base,
            accepted_edits=accepted,
            rejected_edits=len(history) - accepted,
            history=history,
        )


# --- Adaptador de scorer real: tools/migration_linter.py (exit 0/1) -------------


def migration_linter_scorer(
    artifact: str,
    _task: Any = None,
    *,
    linter_path: Optional[Path] = None,
    filename: str = "001_migration.sql",
) -> float:
    """Scorer determinista que usa el linter de migraciones del repo.

    `artifact` = texto SQL (lo que el skill/agente generaría). `_task` se ignora
    (este verificador no depende de la tarea). Devuelve 1.0 si el linter sale 0
    (limpio), 0.0 si sale != 0 (encontró un bug). Sin LLM ni red.
    """
    del _task  # este verificador no depende de la tarea (firma Scorer de 2 args)
    if linter_path is None:
        linter_path = Path(__file__).resolve().parents[2] / "tools" / "migration_linter.py"
    with tempfile.TemporaryDirectory() as td:
        sql = Path(td) / filename
        sql.write_text(str(artifact), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(linter_path), str(sql)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    return 1.0 if proc.returncode == 0 else 0.0
