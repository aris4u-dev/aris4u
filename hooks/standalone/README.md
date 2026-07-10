# Hooks standalone del plugin ARIS4U

Fuentes **VERSIONADAS** de los hooks de Claude Code que forman el valor de gobernanza
del plugin. Un install externo recibe estos hooks completos; sin este directorio, el
instalable solo contiene el dispatcher de ARIS4U — sin gobernanza de modelos, sin
análisis estático, sin control de costes.

Batch L (2026-06-*): governor-context, governor-record.
Batch A1 (2026-07-07): los 7 restantes — P1-M1/M2/M5/M6/M7/M8.
Batch A2 (2026-07-07): ram-saturation-guard + pre-bash-guard — C1 reducido completado.

## Inventario completo

| Hook | Evento | Matcher | Qué hace | Prioridad |
|------|--------|---------|----------|-----------|
| `model-routing-guard.py` | PreToolUse | Workflow\|Agent\|Task | BLOQUEANTE (exit 2): impide Agent/Task/Workflow sin `model=` explícito → protege el 84% del gasto de sesión | P1-M1 |
| `static-analysis-gate.py` | PostToolUse | Write\|Edit\|MultiEdit | Advisory: corre flutter/eslint/astro/gradle sobre el archivo recién editado y reinyecta diagnósticos nivel-IDE | P1-M2 |
| `session-tier-reminder.py` | SessionStart | — | Advisory: si el hilo es Fable, recuerda que es tier reservado a plan maestro/decisión irreversible | P1-M6 |
| `big-output-advisor.py` | PostToolUse | Read\|Bash\|Grep\|Glob | Advisory: si el output es grande, sugiere comprimir vía subagente Sonnet (H3) | P1-M5 |
| `pre-commit-gate.sh` | PreToolUse | Bash | BLOQUEANTE: antes de `git commit`, analiza staged files (Dart/Python/Shell); bloquea si hay errores de compilación | P1-M8 |
| `clarify-gate.py` | UserPromptSubmit | — | Advisory (anti-nag, 1x/sesión): si el prompt pide construir algo, recuerda correr el skill `clarify` antes de planear | P1-M7 |
| `enterprise-build-hint.py` | UserPromptSubmit | — | Advisory (anti-nag, 1x/sesión): en builds complejos/enterprise sugiere el workflow `enterprise-build` | P1-M7 |
| `governor-context.py` | UserPromptSubmit | — | Advisory: inyecta la línea del Gobernador de Concurrencia (nº seguro de agentes + RAM viva) | Batch L |
| `governor-record.py` | SessionEnd | — | Write-path (fail-open + log): registra duraciones de agentes → alimenta la μ continua del gobernador | Batch L |
| `ram-saturation-guard.sh` | PreToolUse | Workflow\|Agent\|Task | BLOQUEANTE (exit 2): impide fan-out pesado cuando RAM disponible < umbral, swap activo, o >= 2 workflows corriendo. Umbrales configurables vía env (RAM_BLOCK_AVAIL_GB etc.). Fail-open en Linux/no-macOS. | Batch A2 |
| `pre-bash-guard.sh` | PreToolUse | Bash | BLOQUEANTE (exit 2) en dos casos: (1) credential scan en git commit — detecta API keys/tokens/keys privadas en staged diff; (2) PHI guard — bloquea egreso a destino externo si el comando contiene términos PHI. Advisory (exit 0) para cluster-impact (apt/docker/systemctl/ollama/rm -rf). `PHI_SAFE_DESTINATIONS` env para añadir hosts locales seguros. | Batch A2 |

## Portabilidad

Todos los hooks:
- Usan `Path.home()` para rutas de usuario (`~/.claude/`) — sin paths hardcodeados.
- Usan `tempfile.gettempdir()` para marcadores de sesión temporales.
- Usan `os.environ.get('CLAUDE_PLUGIN_ROOT')` con fallback `Path(__file__).parents[2]` (governor-*).
- Son FAIL-OPEN (exit 0 ante cualquier error) salvo `model-routing-guard` y `pre-commit-gate`
  que son deliberadamente bloqueantes.

## Despliegue en entorno personal del usuario

El usuario corre sus propias copias desde `~/.claude/hooks/` (cableadas en `~/.claude/settings.json`).
Estos archivos son la **fuente versionada** (historia + rollback + review).

Al actualizar un hook: editar AQUÍ primero, luego copiar:
```bash
cp hooks/standalone/<nombre> ~/.claude/hooks/<nombre>
```

Para regenerar `hooks/hooks.json` desde settings.json:
```bash
python tools/gen_plugin_hooks.py
```

## Registro en hooks.json

`hooks/hooks.json` registra todos estos hooks con rutas `${CLAUDE_PLUGIN_ROOT}/hooks/standalone/`
para que un install externo los cablee automáticamente a los eventos correctos.

Antes (Batch L): 8 hooks totales (dispatcher + bootstrap).
Después (Batch A1): 17 hooks totales (+ 9 standalone).
