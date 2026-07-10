"""Veredicto puro de un handler PreToolUse.

Cada handler es una función pura `(inp) -> Verdict`; NO hace sys.exit. El orquestador
(`dispatch.events.pre_tool_use`) reúne los veredictos y traduce el resultado al contrato
de salida del harness:

  PASS         → no aporta nada
  ADVISE(text) → acumula additionalContext (advisory, exit 0)
  BLOCK(msg)   → contract.block(msg)  → stderr + exit 2  (equivalente a los .sh exit 2)
  DENY(reason) → permissionDecision:"deny" en JSON exit 0 (equivalente a gpu-crash-guard.sh)

BLOCK y DENY cortan la cadena en el PRIMER handler que los devuelva (orden = settings.json).
"""
from __future__ import annotations

from dataclasses import dataclass

PASS = "pass"
ADVISE = "advise"
BLOCK = "block"
DENY = "deny"


@dataclass(frozen=True)
class Verdict:
    """Resultado de un handler. `text` lleva el mensaje (advisory / bloqueo / deny)."""

    kind: str = PASS
    text: str = ""

    @property
    def is_stop(self) -> bool:
        """True si este veredicto debe cortar la cadena (bloqueo o deny)."""
        return self.kind in (BLOCK, DENY)


def ok() -> Verdict:
    return Verdict(PASS, "")


def advise(text: str) -> Verdict:
    return Verdict(ADVISE, text) if text else Verdict(PASS, "")


def block(message: str) -> Verdict:
    return Verdict(BLOCK, message)


def deny(reason: str) -> Verdict:
    return Verdict(DENY, reason)
