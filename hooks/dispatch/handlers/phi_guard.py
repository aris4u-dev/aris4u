"""Handler PreToolUse — phi_guard (BLOQUEANTE, exit 2, SOLO healthcare).

Porta `hooks/phi_guard.sh` 1:1. Bloquea PHI/PII que sale a una API externa, PERO solo
en contexto HEALTHCARE (clientes de salud configurados o ARIS4U_HEALTHCARE=1). Fuera de healthcare =
no-op (PASS), igual que el gate `_aris_healthcare_ctx` del .sh.

Aplica a: Bash, WebFetch, WebSearch. Destinos locales (w2/localhost/ollama/127.0.0.1)
= seguros → PASS aunque haya PHI. PHI + destino externo en healthcare → BLOCK (exit 2).

Pura: devuelve Verdict. Fail-open ante error. El mensaje replica EXACTO el here-doc del .sh.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, UTC
from pathlib import Path

from dispatch.contract import ARIS4U_ROOT
from dispatch.handlers import pre_common
from dispatch.handlers import verdict as V

_NETWORK_TOOLS = {"Bash", "WebFetch", "WebSearch"}

# Patrones PHI (case-insensitive), portados literalmente del array PHI_PATTERNS del .sh.
_PHI_PATTERNS = [
    r"\bpatient[-_ ]?(id|name|history|record)\b",
    r"\bpaciente\b",
    r"\bnombre.del.paciente\b",
    r"\bsocial[-_ ]?security\b",
    r"\bssn\b",
    r"[0-9]{3}-[0-9]{2}-[0-9]{4}",
    r"\bdate.of.birth\b",
    r"\bfecha.de.nacimiento\b",
    r"\bdob\b",
    r"\bmedical[-_ ]?(record|history)\b",
    r"\bhistorial[-_ ]?medico\b",
    r"\bhistoria[-_ ]?clinica\b",
    r"\bhealth[-_ ]?record\b",
    r"\bclinical[-_ ]?note\b",
    r"\bnota[-_ ]?clinica\b",
    r"\bdiagnosis\b",
    r"\bdiagnostico\b",
    r"\btreatment[-_ ]?plan\b",
    r"\bplan[-_ ]?de[-_ ]?tratamiento\b",
    r"\bprognosis\b",
    r"\bpronostico\b",
    r"\bchief[-_ ]?complaint\b",
    r"\bmotivo[-_ ]?de[-_ ]?consulta\b",
    r"\binsurance[-_ ]?id\b",
    r"\bmedicare\b",
    r"\bmedicaid\b",
    r"\bhipaa\b",
    r"\bprotected[-_ ]?health\b",
    r"\bpolicy[-_ ]?number\b",
    r"\bprescription\b",
    r"\breceta[-_ ]?medica\b",
    r"\bdrug[-_ ]?allergy\b",
    r"\blab[-_ ]?result\b",
    r"\bresultado[-_ ]?laboratorio\b",
    r"\bblood[-_ ]?type\b",
    r"\btipo[-_ ]?de[-_ ]?sangre\b",
    r"\bvital[-_ ]?sign\b",
    r"\bsignos[-_ ]?vitales\b",
    r"\bblood[-_ ]?pressure\b",
    r"\bpresion[-_ ]?arterial\b",
    r"\bheart[-_ ]?rate\b",
    r"\bfrecuencia[-_ ]?cardiaca\b",
    r"\bepcr\b",
    r"\brun[-_ ]?report\b",
    r"\bincident[-_ ]?report\b",
    r"\bambulance[-_ ]?crew\b",
    r"\bparamedic[-_ ]?report\b",
    r"\breporte[-_ ]?ambulancia\b",
    r"\bpcr[-_ ]?narrativa\b",
    r"\bmedical[-_ ]?case\b",
    r"\bcaso[-_ ]?medico\b",
    r"\bhome[-_ ]?address\b",
    r"\bdireccion[-_ ]?del[-_ ]?paciente\b",
    r"\bemergency[-_ ]?contact\b",
    r"\bnext[-_ ]?of[-_ ]?kin\b",
    r"\bfamiliar[-_ ]?responsable\b",
]

# Destinos locales seguros (vivos): Mac + W2.
_SAFE_PATTERNS = [
    r"\bw2\b",
    r"100\.112\.134\.86",
    r"192\.168\.4\.200",
    r"\blocalhost\b",
    r"\b127\.0\.0\.1\b",
    r"\bollama\b",
    r"\b11434\b",
    r"ssh[ ]+w2",
]

def _healthcare_path_markers() -> tuple[str, ...]:
    """Construye los marcadores de ruta healthcare desde config (fail-open → tupla vacía).

    El operador configura sus clientes healthcare en ~/.aris4u/config.json:
        {"healthcare_clients": ["cliente-medico-a", "cliente-medico-b"]}

    Con lista vacía (config ausente), el gate de ruta no activa el modo PHI —
    el operador debe usar ARIS4U_HEALTHCARE=1 o el marcador .aris-healthcare.

    Returns:
        Tupla de substrings de ruta a detectar (lower-case).
    """
    try:
        import sys as _sys
        aris_root = Path(__file__).resolve().parents[3]
        aris_root_str = str(aris_root)
        if aris_root_str not in _sys.path:
            _sys.path.insert(0, aris_root_str)
        from engine.v16.config import cfg_healthcare_clients  # noqa: PLC0415
        clients = cfg_healthcare_clients()
        if not clients:
            return ()
        markers: list[str] = []
        for c in clients:
            markers.append(f"03-clients/{c}")
            markers.append(f"/{c}/")
        return tuple(markers)
    except Exception:
        return ()


_HEALTHCARE_PATH_MARKERS: tuple[str, ...] = _healthcare_path_markers()


def _is_healthcare_ctx(cwd: str, tool_text: str = "") -> bool:
    """¿Modo PHI/healthcare ACTIVO? OFF por defecto — solo 3 gates EXPLÍCITOS.

    El operador activa PHI deliberadamente al trabajar con un cliente médico. NUNCA se
    activa por inferencia (texto del prompt / bridge stale en /tmp / env de cliente /
    marker heredado del árbol) — esa activación implícita causaba falsos positivos.

    Activo si y solo si:
      1. ``ARIS4U_HEALTHCARE=1`` — switch maestro (sesión o settings.json env).
      2. marker ``.aris-healthcare`` en el cwd EXACTO (no se hereda subiendo el árbol).
      3. el cwd está literalmente DENTRO de un proyecto cliente healthcare
         (cubre subdirs vía substring; requiere "healthcare_clients" en config.json).

    Args:
        cwd: directorio de trabajo.
        tool_text: IGNORADO — se eliminó la detección por texto (causaba falsos
            positivos); se conserva en la firma por compatibilidad con los llamadores.

    Returns:
        True solo si PHI fue activado por una acción explícita del operador.
    """
    if os.environ.get("ARIS4U_HEALTHCARE") == "1":
        return True
    if cwd and (Path(cwd) / ".aris-healthcare").is_file():
        return True
    if not _HEALTHCARE_PATH_MARKERS:
        return False
    pwd_l = (cwd or "").lower()
    return any(m in pwd_l for m in _HEALTHCARE_PATH_MARKERS)


def _log_audit(tool_name: str, matched: str, is_safe: bool) -> None:
    """Replica el evento de auditoría JSONL del .sh (nunca el PHI en claro).

    session_id se inyecta desde ARIS4U_SESSION_ID (seteado por dispatch.py antes
    de llamar handlers) para que guard_blocks sea contable por sesión (Batch O).
    """
    log_file = ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"
    if not log_file.parent.is_dir():
        return
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    sid = os.environ.get("ARIS4U_SESSION_ID", "")
    try:
        import json
        if is_safe:
            event = {"ts": ts, "hook": "phi_guard", "event": "phi_to_local",
                     "tool": tool_name, "matched": matched, "decision": "allow",
                     "session_id": sid}
        else:
            event = {"ts": ts, "hook": "phi_guard", "event": "phi_to_external_blocked",
                     "tool": tool_name, "matched": matched, "decision": "block",
                     "session_id": sid}
        with open(log_file, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


_BLOCK_MSG = """🛑 ARIS4U PHI GUARD — {tool} blocked

  PHI/PII pattern detected: {pattern}
  Destination is NOT a recognized local cluster target.

  Patient/medical data must NEVER reach external APIs (Claude API, Grok, Gemini, web).
  Run locally instead:
    ollama run qwen35-analyst "<your query>"            # Mac local
    or: ssh w2 'ollama run qwen3:8b "<your query>"'     # W2 local

  Override: mark this tool input with a safe destination (w2/localhost/ollama)
  or sanitize the PHI before invoking the tool."""


# Egress externo: curl/wget/etc o una URL http(s). Precisión 2026-06-24 (consejo 5-lentes):
# un comando LOCAL (grep/git/sed/cat/docker/psql) no puede filtrar PHI a una API externa
# aunque el literal aparezca — era la causa de falsos positivos (ssn en un grep, paciente en
# un mensaje de commit). Solo el egress real (o WebFetch/WebSearch) puede filtrar.
_EGRESS_RE = re.compile(r"\b(curl|wget|nc|telnet|ftp|scp|rsync|httpie)\b|https?://", re.I)


def _is_egress(tool_name: str, text: str) -> bool:
    """WebFetch/WebSearch egresan por naturaleza; Bash solo si invoca red/URL externa."""
    if tool_name in ("WebFetch", "WebSearch"):
        return True
    return bool(_EGRESS_RE.search(text))


def check(tool_name: str, tool_input: dict, cwd: str) -> V.Verdict:
    """Veredicto del phi_guard. BLOCK si PHI→externa en healthcare; si no, PASS."""
    if tool_name not in _NETWORK_TOOLS:
        return V.ok()

    text = pre_common.tool_text(tool_input)
    if not text:
        return V.ok()

    if not _is_healthcare_ctx(cwd or os.getcwd(), text):
        return V.ok()  # no-op fuera de healthcare

    text_lower = text.lower()

    matched_pattern = ""
    for pat in _PHI_PATTERNS:
        if re.search(pat, text_lower):
            matched_pattern = pat
            break
    if not matched_pattern:
        return V.ok()

    # Comando LOCAL sin egress externo → no puede filtrar PHI a una API externa. PASS.
    if not _is_egress(tool_name, text_lower):
        return V.ok()

    # PHI presente Y egresa — ¿destino seguro?
    is_safe = any(re.search(s, text_lower) for s in _SAFE_PATTERNS)
    _log_audit(tool_name, matched_pattern, is_safe)
    if is_safe:
        return V.ok()

    return V.block(_BLOCK_MSG.format(tool=tool_name, pattern=matched_pattern))
