#!/usr/bin/env python3
"""UserPromptSubmit gate: si el prompt pide CONSTRUIR/DISEÑAR algo, inyecta el
recordatorio de correr la fase `clarify` ANTES de planear/construir.

División deliberada de responsabilidad:
  - MECÁNICO (este hook, determinista): detecta INTENCIÓN de build/diseño. Amplio.
  - JUICIO (el skill `clarify` + Claude): decide si el brief tiene ambigüedades
    materiales; si ya está completamente especificado, se salta y pasa a PLAN.

Anti-nag de sesión: dispara UNA sola vez por sesión (marker en tempdir) para no
interrumpir builds consecutivos en la misma conversación.

Decoupled de ARIS4U (no toca dispatch.py). Fail-safe absoluto: ante CUALQUIER
error o no-match → exit 0 silencioso, nunca rompe el prompt path.

Portabilidad: usa tempfile.gettempdir() y os.getppid(). Sin paths hardcodeados.
Fuente versionada: hooks/standalone/clarify-gate.py (ARIS4U plugin).
"""
import sys
import json
import re
import unicodedata
import os
import tempfile


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def main() -> None:
    # B1-ter: headless guard — skip all interactive injections when
    # ARIS4U_HEADLESS=1 is set (e.g. during cowork_runner's claude -p builds).
    if os.environ.get("ARIS4U_HEADLESS") == "1":
        return

    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    prompt = data.get("prompt") or ""
    if not prompt:
        return

    t = _norm(prompt)

    # Verbo de construcción/diseño (amplio, sin acentos).
    build_verb = re.search(
        r"\b(constru|build|desarroll|implementa|hazme|creame|crea(r|me)?|"
        r"haz\b|hacer|armar?|levantar|montar?|disena|genera(r|me)?|"
        r"necesito|quiero|prototip)",
        t,
    )
    # Sustantivo de cosa-a-construir (feature/app/sistema/UI).
    target_noun = re.search(
        r"\b(software|sistema|plataforma|app|aplicacion|saas|erp|crm|portal|"
        r"dashboard|tablero|panel|modulo|backend|frontend|microservic|feature|"
        r"funcionalidad|pantalla|pagina|web\b|sitio|landing|formulario|flujo|"
        r"integracion|api\b|servicio|herramienta|bot|pipeline|reporte|informe)",
        t,
    )
    # Excluir señales de cambio acotado / no-build (reduce falsos positivos).
    trivial = re.search(
        r"\b(arregla|fix|corrige|bug|typo|renombra|rename|formatea|"
        r"explica|que es|como funciona|revisa|audita|debug)",
        t,
    )

    if not (build_verb and target_noun) or trivial:
        return

    # Anti-nag: disparar solo una vez por sesión de Claude.
    # CLAUDE_SESSION_ID no existe como env var → usamos PPID como proxy de sesión.
    # El hook corre como subproceso de Claude Code; os.getppid() = PID de Claude,
    # constante durante toda la sesión y distinto entre sesiones.
    session_key = str(os.getppid())
    marker = os.path.join(tempfile.gettempdir(), f'clarify-gate-nag-{session_key}')
    if os.path.exists(marker):
        sys.exit(0)  # ya avisamos en esta sesión
    open(marker, 'w').close()

    msg = (
        "Gate clarify (capa de proceso): este pedido parece pedir CONSTRUIR/DISEÑAR algo. "
        "ANTES de planear o construir, evalua si el brief tiene ambiguedades materiales; "
        "si las tiene, corre el flujo del skill `clarify` (escaneo de 11 categorias -> "
        "preguntas UNA a la vez via AskUserQuestion -> brief enriquecido listo para PLAN). "
        "Si el brief YA esta completamente especificado, o si ya lo clarificaste en esta "
        "conversacion, dilo brevemente y pasa directo a PLAN/build. No construyas sobre "
        "suposiciones no dichas."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Nunca romper el prompt path.
        pass
    sys.exit(0)
