"""LockedScorer — el "scoring file bloqueado" de Karpathy Auto Research, sobre SkillOpt.

SPIKE (2026-06-29). Injerta en ARIS4U la pieza que el LEGO Learning Engine no tenía:
la SEPARACIÓN FORMAL entre *quién define la métrica* (el humano) y *quién optimiza*
(el agente). En el sistema de 3 archivos de Karpathy, el archivo de scoring está
bloqueado al agente para que no pueda hacerle gaming a su propia evaluación.

`SkillOptLoop` (engine/v16/skillopt.py) ya inyecta un `Scorer` que el optimizador
solo PUEDE LLAMAR, nunca reemplazar. Lo que faltaba: garantizar que la DEFINICIÓN de
ese scorer (su código + sus pesos/umbrales = "el número honesto") no fue alterada
entre el momento en que el humano la selló y el momento en que el loop la usa.

`LockedScorer` lo logra sellando el scorer base + su config tras un manifiesto
SHA-256. Si el código fuente del scorer o su config cambian, `__call__` se NIEGA a
puntuar (anti-tamper) en vez de devolver un número manipulado. Es un `Scorer` válido
(firma `(artifact, task) -> float`), así que entra tal cual en `SkillOptLoop(scorer=...)`.

Política heredada de skillopt.py: "empieza-solo-PR". Esto NO escribe nada; solo sella
y verifica en memoria. El sello se persiste a un manifiesto JSON que un humano commitea.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from collections.abc import Callable

# Mismo contrato que engine/v16/skillopt.py: Scorer = Callable[[Any, Any], float].
Scorer = Callable[[Any, Any], float]
# El scorer BASE puede aceptar kwargs de config (se pasan vía **config al llamar).
BaseScorer = Callable[..., float]


class MetricTamperError(RuntimeError):
    """El scorer sellado fue alterado tras el sello — se rechaza puntuar."""


def _hash_payload(source: str, config: dict[str, Any]) -> str:
    """Huella estable del scorer: su código fuente + su config canónica."""
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    blob = f"{source}\x00{canonical}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class ScoreSeal:
    """Manifiesto inmutable de un scorer sellado (lo que el humano firma y commitea)."""

    metric_id: str
    digest: str
    config: dict[str, Any]
    locked: bool = True

    def to_json(self) -> str:
        return json.dumps(
            {"metric_id": self.metric_id, "digest": self.digest,
             "config": self.config, "locked": self.locked},
            sort_keys=True, indent=2,
        )

    @staticmethod
    def from_json(text: str) -> "ScoreSeal":
        d = json.loads(text)
        return ScoreSeal(d["metric_id"], d["digest"], d["config"], d.get("locked", True))


class LockedScorer:
    """Envuelve un scorer base + su config y los sella contra alteración.

    El optimizador del loop recibe esta instancia como `scorer`: puede LLAMARLA
    (`locked(artifact, task)`) pero no puede reasignar la config ni el scorer base
    sin invalidar el digest, lo que dispara `MetricTamperError` en el siguiente
    `__call__`. Así la métrica queda fuera del alcance del agente que optimiza.
    """

    def __init__(
        self,
        base_scorer: BaseScorer,
        config: dict[str, Any],
        *,
        metric_id: str,
        seal: Optional[ScoreSeal] = None,
    ) -> None:
        self._base = base_scorer
        self._config = dict(config)
        self._metric_id = metric_id
        # Fuente del scorer base: el código mismo es parte de la métrica.
        try:
            self._source = inspect.getsource(base_scorer)
        except (OSError, TypeError):
            self._source = repr(base_scorer)
        live_digest = _hash_payload(self._source, self._config)
        if seal is not None:
            # Modo verificación: el sello debe coincidir con lo que corre AHORA.
            if not seal.locked:
                raise MetricTamperError(f"sello {metric_id} no está locked")
            if seal.digest != live_digest:
                raise MetricTamperError(
                    f"métrica '{metric_id}' alterada tras el sello: "
                    f"esperado {seal.digest[:12]}, vivo {live_digest[:12]}"
                )
            self._seal = seal
        else:
            # Modo sellado: el humano crea el sello inicial.
            self._seal = ScoreSeal(metric_id, live_digest, dict(self._config))

    @property
    def seal(self) -> ScoreSeal:
        return self._seal

    def _verify_integrity(self) -> None:
        live = _hash_payload(self._source, self._config)
        if live != self._seal.digest:
            raise MetricTamperError(
                f"métrica '{self._metric_id}' alterada en runtime: "
                f"sello {self._seal.digest[:12]} != vivo {live[:12]}"
            )

    def __call__(self, artifact: Any, task: Any) -> float:
        """Firma de `Scorer`. Verifica integridad ANTES de devolver el número."""
        self._verify_integrity()
        score = float(self._base(artifact, task, **self._config) if self._config
                      else self._base(artifact, task))
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"score fuera de [0,1]: {score}")
        return score

    def save_seal(self, path: Path) -> Path:
        """Persiste el manifiesto para que un humano lo commitee (no auto-commit)."""
        path.write_text(self._seal.to_json(), encoding="utf-8")
        return path
