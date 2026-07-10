#!/usr/bin/env python3
"""UserPromptSubmit hint: si el prompt parece un 'build serio', sugiere el workflow
`enterprise-build`. Decoupled de ARIS4U (no toca dispatch.py). Fail-safe absoluto:
ante CUALQUIER error o no-match → exit 0 silencioso, nunca rompe el prompt path.
Anti-nag: una sola sugerencia por sesión (marker en tempdir).

Portabilidad: usa tempfile.gettempdir() en vez de /tmp hardcodeado. Sin paths
personales. Fuente versionada: hooks/standalone/enterprise-build-hint.py (ARIS4U plugin).
"""
import sys
import json
import os
import re
import tempfile
import unicodedata


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def main() -> None:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    prompt = data.get("prompt") or ""
    session = data.get("session_id") or "nosess"
    if not prompt:
        return

    t = _norm(prompt)

    # Señales (sobre texto sin acentos).
    build_verb = re.search(r"\b(constru|build|desarroll|implementa|hazme|creame|crear|armar|levantar|montar)", t)
    system_noun = re.search(r"\b(software|sistema|plataforma|app|aplicacion|enterprise|saas|erp|crm|portal|dashboard|modulo|backend|microservic)", t)
    scale_signal = re.search(r"\b(enterprise|completo|de la a a la z|a-z|end.to.end|multi|para clientes?|produccion|regulad|compliance|hipaa|fcra|pci)", t)

    # Conservador: verbo + sustantivo de sistema + (señal de escala O 'enterprise').
    if not (build_verb and system_noun and scale_signal):
        return

    marker = os.path.join(
        tempfile.gettempdir(),
        f"eb-hint-{re.sub(r'[^A-Za-z0-9_-]', '', str(session))}"
    )
    if os.path.exists(marker):
        return
    try:
        open(marker, "w").close()
    except Exception:
        pass

    msg = (
        "Pista (capa de proceso, opt-in): este pedido parece un build serio/complejo. "
        "Para regulado/multi-modulo conviene el workflow `enterprise-build` "
        "(DISCOVER->CONTRACT->BUILD+reflexion->VERIFY mecanico->SYNTHESIZE) que cierra el gap "
        "research->build probado el 2026-06-22. Invocalo con la skill `enterprise-build` o "
        "Workflow({name:'enterprise-build', args:{domain, spec, modules, outDir}}). "
        "Ignora esta pista si es un cambio acotado."
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
