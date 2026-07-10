---
name: aris-onboard
description: |
  Onboarding guiado de ARIS4U DENTRO de Claude Code — reemplaza la guía HTML externa.
  Lleva a un usuario nuevo de "plugin recién instalado" a "ARIS4U 100% funcional" en un
  solo flujo conversacional: detecta el estado actual (venv, config, env vars, Ollama),
  completa lo que falte, muestra EXACTAMENTE qué variables de entorno pegar en
  ~/.claude/settings.json (el único paso que un plugin no puede automatizar), y verifica
  el resultado con /aris-status. Idempotente: correrlo de nuevo solo reporta lo que falta.
  When to use: (1) justo después de `/plugin install aris4u@…`, (2) tras cambiar de máquina,
  (3) para diagnosticar por qué ARIS4U no parece activo. Example: `/aris-onboard`
---

## Propósito

Este skill es el **onboarding auto-contenido** de ARIS4U. Antes existía una guía HTML
externa con 6 prompts para copiar-pegar; este skill absorbe esos 6 pasos y los ejecuta
como una conversación guiada, sin que el usuario salte entre la terminal y un documento.

Objetivo final: el usuario pasa de *plugin instalado* a *ARIS4U funcional y verificado*.

## Postura de ejecución

Actúa como un asistente de instalación. En cada paso: **detecta primero, actúa solo si
falta, y reporta el resultado**. Nunca pises configuración existente del usuario. Todo lo
que escribas en `~/.claude/settings.json` debe FUSIONAR, jamás reemplazar.

## Flujo (6 pasos — detecta → completa → verifica)

### Paso 1 — Entorno Python (venv + dependencias)
El hook `SessionStart` (`bootstrap_venv.sh`) ya crea `.venv312` en la primera sesión.
Verifica que existe y que las dependencias cargan:

```bash
ls -d "${CLAUDE_PLUGIN_ROOT:-$HOME/projects/aris4u}"/.venv312/bin/python3 2>/dev/null \
  && echo "venv OK" || echo "FALTA venv — corre: bash install.sh"
```

Si falta, corre `bash "${CLAUDE_PLUGIN_ROOT}/install.sh" --yes` y diagnostica cualquier fallo
(Python < 3.11, permisos, red). No sigas hasta que el smoke-test del contrato pase.

### Paso 2 — Config por-usuario (`~/.aris4u/config.json`)
ARIS4U desacopla hardware/clientes/proyectos de esta máquina en un config generado, NO en
código. Genéralo (idempotente, no destructivo):

```bash
"${CLAUDE_PLUGIN_ROOT}/.venv312/bin/python3" "${CLAUDE_PLUGIN_ROOT}/tools/aris4u_init.py" --yes
```

Luego **muestra al usuario** qué detectó: hardware (cores/RAM/chip), proyectos escaneados en
`~/projects/`, y clientes. Confírmale que puede editar `~/.aris4u/config.json` o regenerar con
`--force`. (Equivale al prompt 2 de la guía vieja.)

### Paso 3 — Registro del plugin
Si el usuario llegó por marketplace nativo ya está instalado. Si instaló desde un clon local,
regístralo:

```bash
claude plugin marketplace add https://github.com/aris4u-dev/aris4u.git   # o la ruta local del clon
claude plugin install aris4u@aris4u-dev
```

Recuérdale correr `/reload-plugins` una vez para activar hooks + MCP + agents + skills.

### Paso 4 — Variables de entorno (merge automático disponible)
ARIS4U incluye `tools/settings_merge.py`, un helper que fusiona el bloque env de forma
IDEMPOTENTE: solo añade las claves que faltan, preserva todo lo existente, y hace backup
automático de `settings.json` antes de tocar nada.

Ofrécele al usuario dos opciones:

**Opción A — merge automático (recomendado):**

```bash
# Ver qué añadiría (dry-run, sin tocar nada):
"${CLAUDE_PLUGIN_ROOT}/.venv312/bin/python3" "${CLAUDE_PLUGIN_ROOT}/tools/settings_merge.py"

# Aplicar (backup automático en settings.json.bak):
"${CLAUDE_PLUGIN_ROOT}/.venv312/bin/python3" "${CLAUDE_PLUGIN_ROOT}/tools/settings_merge.py" --apply
```

Si ya corrió antes y no falta nada, imprime "Nothing to merge — already up to date." (idempotente).

**Opción B — manual (por si prefiere revisar antes de dejar que Claude toque settings.json):**
Muéstrale el bloque EXACTO y dile que lo pegue en la sección `env` de `~/.claude/settings.json`
(añadiendo solo las claves que NO tenga ya):

```jsonc
{
  "env": {
    // — rendimiento y memoria —
    "ENABLE_PROMPT_CACHING_1H": "true",
    "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "15000",

    // — ARIS4U: profundidad, verificación, privacidad —
    "ARIS4U_DEPTH_PROTOCOL": "1",
    "ARIS4U_CONDUCTOR_ENFORCE": "1",
    "ARIS4U_HEALTHCARE": "0",

    // — opcionales (ajuste fino) —
    "ARIS4U_ROUTER_SEM_THRESHOLD": "0.70",
    "ARIS4U_DIVERSE_RECALL": "0"
  }
}
```

Qué significa cada una:
| Variable | Efecto |
|----------|--------|
| `ENABLE_PROMPT_CACHING_1H` | Caché de prompt de 1h — más barato y rápido. |
| `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | Da tiempo al SessionEnd para capturar el digest. |
| `ARIS4U_DEPTH_PROTOCOL` | Activa la clasificación de profundidad por query. |
| `ARIS4U_CONDUCTOR_ENFORCE` | Recordatorio suave del ciclo ENTENDER→DISEÑAR→CONSTRUIR→VERIFICAR. |
| `ARIS4U_HEALTHCARE` | `0` = PHI OFF (default). Ponlo en `1` solo si trabajas con datos clínicos. |
| `ARIS4U_ROUTER_SEM_THRESHOLD` | Umbral del router semántico de capacidades (0.70 = balanceado). |
| `ARIS4U_DIVERSE_RECALL` | Diversifica el recall de memoria (experimental). |
| `ARIS4U_AUTOPILOT` | **Solo si el usuario NO es dev y quiere hands-off total.** En `1`, cuando su petición corresponda a una capacidad conocida, Claude la **ejecuta** en vez de solo sugerirla. Default `0` (sugerencias). |

Tras fusionar, avísale que debe **reiniciar la sesión** para que Claude Code recargue `env`.
Para un no-desarrollador que quiere la experiencia "yo hablo, ustedes ejecutan de la A a la Z",
ofrécele añadir `ARIS4U_AUTOPILOT=1`.

### Paso 5 — Ollama + índice semántico (activa "hablo → capacidad correcta")
ARIS4U degrada limpio sin Ollama (FTS5 + guards siguen funcionando), pero con embeddings
locales se enciende lo más valioso para un no-desarrollador: el **router semántico**, que
reconoce la intención de lo que el usuario pide y sugiere/usa la capacidad correcta sin que
tenga que nombrarla. Si el usuario quiere esa automatización plena:

```bash
curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && echo "Ollama OK" \
  || echo "Ollama no detectado — instálalo desde ollama.com"
ollama pull mxbai-embed-large    # embeddings PHI-safe locales
# construye el índice de capacidades — SIN esto el router semántico queda a medias:
"${CLAUDE_PLUGIN_ROOT:-$HOME/projects/aris4u}/.venv312/bin/python3" \
  "${CLAUDE_PLUGIN_ROOT:-$HOME/projects/aris4u}/tools/capability_semantic.py" --build
```

Sin Ollama, el router cae a coincidencia por palabra clave (cobertura menor) — funciona,
pero para la experiencia "solo hablo y se ejecuta" el índice semántico es el que marca la
diferencia.

### Paso 6 — Verificación + primer uso real
Cierra con la verificación mecánica y una demo:

```bash
# estado del sistema (hooks + MCP + guards + memoria)
/aris-status
```

Luego demuestra la memoria por-cliente en vivo: usa `aris_ingest` para guardar una decisión
de prueba y `aris_recall_client` para recuperarla, así el usuario ve el bucle de memoria
funcionando. (Equivale al prompt 6 de la guía vieja.)

**Cierre para el usuario (dilo en lenguaje llano):** a partir de ahora **no necesita aprender
comandos**. Solo describe en lenguaje natural lo que quiere o necesita — ARIS4U reconoce la
intención y Claude usa las herramientas correctas (skills, memoria, verificación, agentes) de
principio a fin. Él se enfoca en el *qué*; ARIS4U + Claude se encargan del *cómo*.

## Criterio de "onboarding completo"

Reporta ✅ solo cuando: (1) `.venv312` existe y el contrato carga, (2) `~/.aris4u/config.json`
generado, (3) plugin registrado + `/reload-plugins` corrido, (4) env vars fusionadas en
settings.json, (5) `/aris-status` muestra hooks + MCP + memoria vivos. Ollama + índice
semántico son opcionales pero son lo que da la experiencia plena "solo hablo y se ejecuta".

## Notas

- **Idempotente**: correr `/aris-onboard` de nuevo solo detecta y completa lo que falte.
- **No destructivo**: nunca sobrescribe `settings.json`, `config.json` ni el venv del usuario.
  `settings_merge.py` hace backup `.bak` antes de cualquier escritura.
- El Paso 4 ya tiene merge automático via `tools/settings_merge.py --apply`. El usuario puede
  elegir el merge automático (recomendado) o manual si prefiere revisar el diff antes.
- Si el usuario trabaja con un cliente médico, guíalo a `ARIS4U_HEALTHCARE=1` o el skill
  `/aris-config --healthcare on` — nunca lo actives por defecto.
