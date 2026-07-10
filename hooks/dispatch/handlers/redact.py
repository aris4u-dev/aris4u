"""Handler redact_secrets — portado de hooks/redact_secrets.sh (PostToolUse Bash).

MUTA el output del tool: redacta credenciales (AWS, Firebase, Supabase JWT, GCP
private key, env genéricos) y devuelve el output saneado vía `updatedToolOutput`.
Es el ÚNICO sub-handler de PostToolUse que altera el resultado del tool.

Contrato preservado EXACTO del .sh viejo (incluye el fix JSONL-safe reciente:
los valores se cortan en delimitadores JSON ` "',}`, no solo whitespace, para no
romper líneas JSONL compactas). Si no es Bash o no hay secretos → no muta.

`redact(tool_name, tool_output) -> (updated_output | None, total_redacted)`:
  - updated_output is None  → no mutación (passthrough de output)
  - updated_output is str    → output saneado (≠ original)
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# Patrones equivalentes a los grep/sed del .sh. Se usan substituciones Python con
# clase de caracteres JSONL-safe [^ "',}] donde el .sh corta en delimitadores.
_JSONL_STOP = r"[^ \"',}]"  # mismo conjunto que [^ ",'}] del sed -E del .sh

_AWS_ACCESS_KEY = re.compile(r"AKIA[0-9A-Z]{16}")
_FIREBASE_KEY = re.compile(r"AIza[A-Za-z0-9_-]{35}")
_SUPABASE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*\.[A-Za-z0-9_-]*")
_AWS_SECRET = re.compile(r"aws_secret_access_key=" + _JSONL_STOP + r"*", re.IGNORECASE)
_GCP_PRIVATE_KEY = re.compile(
    r"-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----", re.DOTALL
)
# Genéricos PASSWORD=/API_KEY=/… — el .sh redacta solo valores de >=16 chars,
# tras descartar localhost/127.0.0.1/example.com y password=true|false|0|1|null.
_GENERIC_KEYS = "(?:PASSWORD|API_KEY|SECRET_TOKEN|OAUTH_TOKEN|BEARER|DB_PASSWORD)"
_GENERIC_DETECT = re.compile(_GENERIC_KEYS + r"=", re.IGNORECASE)
_GENERIC_VALUE = re.compile(_GENERIC_KEYS + r"=(" + _JSONL_STOP + r"{16,})", re.IGNORECASE)
_GENERIC_SKIP = re.compile(r"localhost|127\.0\.0\.1|example\.com", re.IGNORECASE)
_GENERIC_TRIVIAL = re.compile(
    r"password=(?:true|false|0|1|null)", re.IGNORECASE
)


def redact(tool_name: str, tool_output: str) -> Tuple[Optional[str], int]:
    """Replica redact_secrets.sh. Devuelve (updated_output|None, total_redacted).

    Args:
        tool_name: nombre del tool (solo se procesa "Bash", igual que el .sh).
        tool_output: salida cruda del tool.

    Returns:
        (None, 0) si no aplica o no hay secretos; (str_saneado, n) si redactó.
    """
    if tool_name != "Bash":
        return None, 0
    if not tool_output:
        return None, 0

    out = tool_output
    total = 0

    # Pattern 1: AWS access key
    n = len(_AWS_ACCESS_KEY.findall(out))
    if n:
        total += n
        out = _AWS_ACCESS_KEY.sub("[REDACTED:aws_access_key]", out)

    # Pattern 2: Firebase API key
    n = len(_FIREBASE_KEY.findall(out))
    if n:
        total += n
        out = _FIREBASE_KEY.sub("[REDACTED:firebase_api_key]", out)

    # Pattern 3: Supabase JWT
    n = len(_SUPABASE_JWT.findall(out))
    if n:
        total += n
        out = _SUPABASE_JWT.sub("[REDACTED:supabase_jwt]", out)

    # Pattern 4: AWS secret (case-insensitive, JSONL-safe value)
    n = len(_AWS_SECRET.findall(out))
    if n:
        total += n
        out = _AWS_SECRET.sub("[REDACTED:aws_secret]", out)

    # Pattern 5: GCP private key block
    n = len(_GCP_PRIVATE_KEY.findall(out))
    if n:
        total += n
        out = _GCP_PRIVATE_KEY.sub("[REDACTED:gcp_private_key]", out)

    # Pattern 6: genéricos env (PASSWORD=, API_KEY=, …), con guards de falso-positivo.
    if _GENERIC_DETECT.search(out):
        # Conteo equivalente al pipeline grep -io | grep -v … | wc -l del .sh:
        # match de pares clave=valor (cualquier longitud, JSONL-safe), menos
        # los triviales (true/false/0/1/null) y los hostnames conocidos.
        detect_re = re.compile(_GENERIC_KEYS + r"=" + _JSONL_STOP + r"*", re.IGNORECASE)
        count = 0
        for m in detect_re.findall(out):
            if _GENERIC_TRIVIAL.search(m):
                continue
            if _GENERIC_SKIP.search(m):
                continue
            count += 1
        if count > 0:
            # El .sh suma `count` (matches de cualquier longitud, menos guards) a
            # TOTAL_REDACTED, pero la sustitución solo toca valores de >=16 chars
            # ({16,}). Quirk preservado EXACTO: un valor corto cuenta como redactado
            # aunque el output quede idéntico.
            total += count
            out = _GENERIC_VALUE.sub(
                lambda mm: mm.group(0)[: mm.start(1) - mm.start(0)] + "[REDACTED]", out
            )

    if total > 0:
        return out, total
    return None, 0
