#!/usr/bin/env python3
"""Driver del loop SkillOpt — política "empieza-solo-PR" (no auto-merge).

Cierra el ciclo de auto-mejora de skills SIN romper el canon: el optimizador real
(proponer la EDICIÓN) es Claude/el usuario, NO un modelo local. El modelo local (MLX)
solo REFLEJA — convierte fallos verificables en FLAGS baratos ("qué le falta al
skill"), igual que `aris_critique`.

Dos modos:
  propose  <skill.md>                 rollout -> score (migration_linter) -> reflect
                                      (MLX flags, fail-open) -> escribe
                                      <skill>.skillopt-proposal.md para que Claude/el usuario
                                      redacte la edición.
  validate <skill.md> <candidate.md>  pasa el candidato por el validation-gate
                                      (mejora estricta en held-out) -> ACCEPT/REJECT.

Verificador = `tools/migration_linter.py` (exit 0/1), el ground truth real del repo.
Uso:
  .venv312/bin/python tools/skillopt_propose.py propose skills/aris-client-audit/SKILL.md
  .venv312/bin/python tools/skillopt_propose.py validate skills/.../SKILL.md /tmp/cand.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import engine.v16.model_router as model_router  # noqa: E402 (tras sys.path)
from engine.v16.skillopt import SkillOptLoop, migration_linter_scorer  # noqa: E402

try:  # persistencia opcional (no acoplar el driver a la DB)
    from engine.v16.soft_reward_loop import record_reward as _record_reward
except Exception:  # pragma: no cover - entorno sin DB
    _record_reward = None

# --- Corpus demo verificable (dominio migraciones) -----------------------------
# El "agente" real (skill -> SQL) es Claude en producción. Para que el driver corra
# out-of-the-box se usa un agente demo determinista keyed en si el skill trae la regla.
_RULE_MARKER = "now() en el predicado where de un indice parcial"
_BAD_SQL = "CREATE INDEX idx_recent ON events (id) WHERE created_at > NOW();\n"
_GOOD_SQL = "CREATE TABLE users (id bigint PRIMARY KEY, email text NOT NULL);\n"
_DEMO_TASKS: list[dict[str, Any]] = [
    {"id": i, "request": "crea un indice para filas recientes de events"} for i in range(8)
]


def _demo_agent(skill_text: str, _task: Any) -> str:
    """Agente demo: emite SQL limpio si el skill contiene la regla; si no, el buggy."""
    del _task
    return _GOOD_SQL if _RULE_MARKER in skill_text.lower() else _BAD_SQL


def _mlx_reflector(skill_text: str, failures: list[Any]) -> list[str]:
    """REFLECT: fallos -> FLAGS vía MLX local (fail-open si está frío). NO edita."""
    del skill_text
    if not failures:
        return []
    sample = _demo_agent("", failures[0])  # el SQL que falla
    prompt = (
        "Un linter de migraciones rechazo este SQL:\n"
        f"{sample}\n"
        "En vinetas, lista QUE REGLA le falta a un skill de migraciones para evitar "
        "este bug. Solo las reglas, sin preambulo."
    )
    res = model_router.route_local("skillopt_reflect", prompt, timeout=40)
    if res.ok and res.text:
        return [ln.strip("-* \t") for ln in res.text.splitlines() if ln.strip()]
    # fail-open (canon: MLX frio -> no degradar, devolver el fallo crudo)
    return [f"(MLX frio) {len(failures)} caso(s) fallan el linter; revisar predicados de indice"]


def _render_proposal(skill_path: str, base: float, n_fail: int, flags: list[str]) -> str:
    flags_md = "\n".join(f"- {f}" for f in flags) if flags else "- (sin flags)"
    return (
        f"# SkillOpt — propuesta de edicion\n\n"
        f"**Skill:** `{skill_path}`\n"
        f"**Score base (verificado):** {base:.3f}  ·  **casos que fallan:** {n_fail}\n\n"
        f"## FLAGS (reflect local, MLX) — que le falta al skill\n{flags_md}\n\n"
        f"## Edicion propuesta (la redacta Claude/el usuario — empieza-solo-PR)\n"
        f"> Edita el skill para incorporar las reglas de arriba y vuelve a correr:\n"
        f"> `skillopt_propose.py validate {skill_path} <candidato.md>`\n"
        f"> El gate aceptara la edicion SOLO si mejora estricto el score en held-out.\n"
    )


def cmd_propose(skill_path: str) -> int:
    skill = Path(skill_path).read_text(encoding="utf-8")
    loop = SkillOptLoop(_demo_agent, migration_linter_scorer, lambda *_: None, reflector=_mlx_reflector)
    base = loop.evaluate(skill, _DEMO_TASKS)
    failures = [t for t in _DEMO_TASKS if migration_linter_scorer(_demo_agent(skill, t)) < 1.0]
    flags = _mlx_reflector(skill, failures)
    out = Path(skill_path).with_suffix(".skillopt-proposal.md")
    out.write_text(_render_proposal(skill_path, base, len(failures), flags), encoding="utf-8")
    print(f"[propose] base={base:.3f} fallos={len(failures)} flags={len(flags)} -> {out}")
    return 0


def cmd_validate(skill_path: str, candidate_path: str) -> int:
    skill = Path(skill_path).read_text(encoding="utf-8")
    candidate = Path(candidate_path).read_text(encoding="utf-8")
    sink = _record_reward if _record_reward is not None else None
    loop = SkillOptLoop(
        _demo_agent, migration_linter_scorer, lambda *_: candidate, reward_sink=sink,
        skill_id=Path(skill_path).stem,
    )
    res = loop.step(skill, _DEMO_TASKS[:5], _DEMO_TASKS[5:])
    verdict = "ACCEPT" if res.accepted else "REJECT"
    print(f"[validate] {verdict}: {res.reason} (base={res.old_score:.3f} cand={res.new_score:.3f})")
    return 0 if res.accepted else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Driver del loop SkillOpt (empieza-solo-PR)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("propose", help="rollout->score->reflect->emite propuesta")
    pp.add_argument("skill")
    pv = sub.add_parser("validate", help="pasa un candidato por el validation-gate")
    pv.add_argument("skill")
    pv.add_argument("candidate")
    args = p.parse_args(argv)
    if args.cmd == "propose":
        return cmd_propose(args.skill)
    return cmd_validate(args.skill, args.candidate)


if __name__ == "__main__":
    raise SystemExit(main())
