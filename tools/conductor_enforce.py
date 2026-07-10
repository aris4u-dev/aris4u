#!/usr/bin/env python3
"""Verify-gate SUAVE al CERRAR el turno (Fase 4 — OFF por defecto, NUNCA bloquea).

VERIFICACIÓN DE CÓDIGO = pilar nº1 del producto. Los gates ``code_quality_gate`` (ruff
por-edit), ``commit_quality_gate`` (pyright + tests por-commit) y ``migration_linter``
verifican DURANTE el trabajo. Este módulo cierra el lazo AL CERRAR el turno: para
intenciones donde se PRODUCE código (``implementation`` / ``fix``), si la sesión tocó
código y NADIE lo verificó (no se corrieron tests/lint/types, no se invocó un gate de
cierre del inventario), produce un RECORDATORIO suave para inyectar en Stop.

DISEÑO (decisión, no atajo):
  - SUAVE, NO BLOQUEANTE: el recordatorio se emite como additionalContext en Stop; NUNCA
    ``{"decision":"block"}``. El bloqueo HARD obligaba al modelo a continuar (intrusivo);
    aquí solo se RECUERDA. Por eso es seguro de activar.
  - DETERMINISTA, OFF POR DEFECTO: la señal (¿se tocó código sin verificar?) es barata,
    fail-open y suficiente para un nudge conservador; no llama a un modelo en el hot path
    de Stop. Se ACTIVA con ``ARIS4U_CONDUCTOR_ENFORCE=1``; sin el flag, ``maybe_reminder``
    devuelve ``""`` siempre y una sesión normal queda intacta.
  - GENÉRICO: las señales de verificación son de PRIMERA PARTE (tests/lint/types nativos +
    capacidades de cierre del inventario filtradas aguas arriba). CERO nombres de cliente.
  - FAIL-OPEN: cualquier duda → ``""`` (no molestar).

Las señales runtime (¿se tocó código? ¿se verificó?) las recolecta ``tools.verify_gate``
desde PostToolUse; aquí solo se aplica la POLÍTICA.
"""
from __future__ import annotations

import os

# Intenciones donde se PRODUCE código y el gate de cierre importa.
_ENFORCED_INTENTS = frozenset({"implementation", "fix"})

# Capacidades de la fase VERIFICAR adoptadas vía hint (espejo de orchestration_protocol y
# verify_gate). Se comparan por HOJA (último segmento) para tolerar prefijos de plugin/servidor.
_VERIFY_LEAVES = frozenset(
    {"second-auditor", "code-review", "verify-claims", "aris_dialectic", "review"}
)

_REMINDER = (
    "🧭 RECORDATORIO de cierre (intent={intent}): tocaste código y no veo señal de "
    "verificación. Antes de declarar 'listo' pasa por la fase VERIFICAR: corre los tests "
    "afectados + lint/types (gates nativos), o usa el gate de cierre de tu inventario "
    "(second-auditor / code-review / verify-claims / aris_dialectic). No declares 'listo' "
    "sin verificar mecánicamente. (Recordatorio SUAVE, no un bloqueo; desactiva con "
    "ARIS4U_CONDUCTOR_ENFORCE=0.)"
)


def _leaf(name: str) -> str:
    """Último segmento de un nombre tras ``.`` / ``:`` / ``__`` (minúsculas)."""
    out = name.lower()
    for sep in (".", ":", "__"):
        out = out.split(sep)[-1]
    return out


def is_enforce_on() -> bool:
    """¿El verify-gate está activado? OFF salvo ``ARIS4U_CONDUCTOR_ENFORCE=1``."""
    return os.environ.get("ARIS4U_CONDUCTOR_ENFORCE", "0").strip() == "1"


def used_verify_phase(adopted_names: list[str]) -> bool:
    """¿Alguna capacidad adoptada vía hint este turno es de la fase VERIFICAR?"""
    leaves = {_leaf(n) for n in adopted_names if n}
    return bool(leaves & _VERIFY_LEAVES)


def _should_remind(
    intent: str, adopted_names: list[str], code_touched: bool, native_verified: bool
) -> bool:
    """Política pura: ¿corresponde recordar la verificación al cerrar?

    Recuerda solo si la intención produce código, hubo edición de código y NO hay ninguna
    señal de verificación (ni capacidad de cierre adoptada, ni tests/lint nativos).

    Args:
        intent: Intención F1 del turno.
        adopted_names: Capacidades adoptadas vía hint este turno (telemetría Fase 4).
        code_touched: Si la sesión editó algún archivo de código (verify_gate).
        native_verified: Si se corrió una verificación nativa (tests/lint/types) (verify_gate).

    Returns:
        True si corresponde el recordatorio suave; False en otro caso.
    """
    if intent not in _ENFORCED_INTENTS:
        return False
    if not code_touched:
        return False  # no se tocó código → nada que verificar
    if used_verify_phase(adopted_names) or native_verified:
        return False  # ya se verificó (capacidad de cierre o tests/lint)
    return True


def build_stop_reminder(
    intent: str,
    adopted_names: list[str],
    code_touched: bool = True,
    native_verified: bool = False,
) -> str:
    """Recordatorio suave si un turno con código no cerró con verificación (función PURA).

    No mira el flag ni hace I/O: la decisión de inyectarlo (gate del flag) y la lectura de
    las señales runtime viven en ``maybe_reminder``. ``code_touched`` por defecto True para
    que la política se pueda probar de forma aislada.

    Args:
        intent: Intención F1 del turno.
        adopted_names: Capacidades de cierre adoptadas vía hint este turno.
        code_touched: Si se editó código este turno.
        native_verified: Si se corrió una verificación nativa (tests/lint/types).

    Returns:
        El texto del recordatorio, o ``""`` si no aplica.
    """
    if not _should_remind(intent, adopted_names, code_touched, native_verified):
        return ""
    return _REMINDER.format(intent=intent)


def maybe_reminder(intent: str, adopted_names: list[str], session_id: str = "") -> str:
    """``build_stop_reminder`` GATEADO por el flag + señales runtime de ``verify_gate``.

    Único punto que el hook Stop debe llamar: con el flag OFF (por defecto) devuelve ``""``
    y la sesión normal no se ve afectada. Con el flag ON, lee del ``verify_gate`` si se tocó
    código y si se corrió verificación nativa, y decide el recordatorio. Fail-open: si
    ``verify_gate`` no está disponible, las señales degradan a "seguro" (no molestar).

    Args:
        intent: Intención F1 del turno.
        adopted_names: Capacidades de cierre adoptadas vía hint este turno.
        session_id: Sesión a inspeccionar en ``verify_gate``.

    Returns:
        El texto del recordatorio a inyectar (vacío si OFF / no aplica).
    """
    if not is_enforce_on():
        return ""
    code_touched = True
    native_verified = False
    try:
        from tools import verify_gate

        code_touched = verify_gate.code_was_touched(session_id)
        native_verified = verify_gate.verification_ran(session_id)
    except Exception:
        # Sin verify_gate, degrada conservador: code_touched=True deja que la señal de
        # capacidad adoptada (used_verify_phase) gobierne; nunca rompe.
        pass
    return build_stop_reminder(intent, adopted_names, code_touched, native_verified)
