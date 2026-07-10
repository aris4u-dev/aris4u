"""Capa de orquestación formal de ARIS4U (§8.4-8.5 del LOCAL_AMPLIFIER_BLUEPRINT).

Dos niveles:
  • Bibliotecas matemáticas PURAS (diferidas, sin estado): ``queueing``, ``markov``,
    ``decision``, ``calibration``. Modelan la ejecución del orquestador y validan el sensor.
  • Capa de ASESORES DISCIPLINADOS sobre esas libs (2026-07-01): ``capacity_advisor``,
    ``markov_advisor``, ``decision_advisor``, ``calibration_advisor`` — cada uno exige datos
    suficientes (o REHÚSA), declara supuestos, y devuelve rango/veredicto. Y
    ``concurrency_governor``, CABLEADO EN VIVO (hooks UserPromptSubmit/SessionEnd) para
    dimensionar el fan-out de agentes.

Contrato de asesor (mínimo, con desviaciones justificadas por dominio y documentadas):
    ``result.refused: bool`` + ``result.caveats: tuple[str, ...]`` en capacity/markov;
    ``decision_advisor`` OMITE ``refused`` (payoffs son input del usuario, no datos medidos);
    ``calibration_advisor`` delega en ``calibration.SensorVerdict`` (la disciplina —rehúso,
    falla-cerrado— ya vive en la lib base, sería redundante reimplementarla).

IMPORTS: este ``__init__`` NO importa nada (evita cargar numpy/scipy al importar el paquete
→ mantiene barato el hot-path del gobernador, que corre en cada prompt vía hook). Cada
submódulo se importa explícitamente donde se necesita: ``from . import queueing`` etc.
"""
