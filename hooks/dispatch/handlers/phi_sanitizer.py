"""Handler PreToolUse — phi_sanitizer (advisory, SOLO healthcare).

Porta `hooks/phi_sanitizer.sh`: versión NO bloqueante de phi_guard. Detecta PHI tier-1
(SSN/DOB/MRN/NPI) en Bash/Write/Edit/Read y, en contexto healthcare, emite un aviso
(advisory) + log de auditoría. Fuera de healthcare = no-op. NUNCA bloquea.

Reusa el gate de contexto healthcare de `phi_guard` (idéntico al del .sh). Pura → Verdict.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone, UTC

from dispatch.contract import ARIS4U_ROOT
from dispatch.handlers import phi_guard as _pg
from dispatch.handlers import pre_common
from dispatch.handlers import verdict as V

_TOOLS = {"Bash", "Write", "Edit", "Read"}

# Patrones tier-1 del .sh (sobre texto en minúsculas).
_SSN_RE = re.compile(r"[0-9]{3}-[0-9]{2}-[0-9]{4}")
_DOB_RE = re.compile(
    r"(0[1-9]|1[0-2])[-/](0[1-9]|[12][0-9]|3[01])[-/](19[0-9]{2}|200[0-9]|201[0])"
)
_MRN_RE = re.compile(r"mrn[-:]?\s*[0-9]{5,10}")
_NPI_RE = re.compile(r"npi[-:]?\s*[0-9]{10}")


def _detect(text_lower: str) -> str:
    if _SSN_RE.search(text_lower):
        return "SSN"
    if _DOB_RE.search(text_lower):
        return "DOB"
    if _MRN_RE.search(text_lower):
        return "MRN"
    if _NPI_RE.search(text_lower):
        return "NPI"
    return ""


def _log(tool_name: str, pattern: str) -> None:
    log_file = ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"
    if not log_file.parent.is_dir():
        return
    try:
        import json
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        event = {"event": "phi_detected", "hook": "phi_sanitizer",
                 "tool_name": tool_name, "pattern": pattern, "timestamp": ts,
                 "session_id": ""}
        with open(log_file, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass


def check(tool_name: str, tool_input: dict, cwd: str) -> V.Verdict:
    """Veredicto phi_sanitizer (siempre advisory). PASS fuera de healthcare o sin PHI."""
    if tool_name not in _TOOLS:
        return V.ok()
    text = pre_common.tool_text(tool_input)
    if not text:
        return V.ok()
    if not _pg._is_healthcare_ctx(cwd, text):
        return V.ok()
    pattern = _detect(text.lower())
    if not pattern:
        return V.ok()
    _log(tool_name, pattern)
    return V.advise(
        f"[PHI-SANITIZER] PHI detected in {tool_name} tool call\n"
        f"  Pattern detected: {pattern}\n"
        "  Recommendation: Route sensitive queries to local Ollama (qwen35-analyst) "
        "instead of external APIs\n"
        "  For pentesting data: use ssh w2 (Foundation-Sec/xploiter) or local Ollama "
        "for uncensored inference"
    )
