<div align="center">

# ARIS4U

### El amplificador de Claude Code — memoria, gobernanza y un *cowork* para developers

*La cognición la renta Claude. ARIS4U potencia el canal: recuerda, hace cumplir tus estándares, y convierte a Claude en un producto que otros pueden operar.*

`plugin v18.0.0` · Claude Code · Python · BSL-1.1

</div>

---

## ¿Qué es ARIS4U? (en 30 segundos)

Claude Code es potentísimo, pero **olvida entre sesiones**, **no conoce tus estándares**, y **un no-técnico no puede juzgar lo que produce**. ARIS4U resuelve exactamente eso: es un plugin que se instala sobre Claude Code y le añade **memoria persistente por-cliente**, **guards que hacen cumplir tus reglas antes de que pases un error**, **trazabilidad de cada decisión**, y un **"cowork"** donde una persona no-técnica describe lo que quiere, ve a Claude+ARIS4U **construirlo en vivo**, y los developers colaboran encima — todo gobernado.

> **La idea central:** no reemplaza la inteligencia de Claude — la **amplifica y la hace gobernable**. Claude piensa; ARIS4U recuerda, protege y explica.

---

## Los problemas que resuelve

| El dolor | Cómo lo resuelve ARIS4U |
|---|---|
| 🧠 **"Claude olvida todo entre sesiones."** | Memoria cross-session por-cliente (FTS5 + semántica). Recupera tus decisiones, guards y contexto de cada proyecto automáticamente en cada prompt. |
| 🛡️ **"Claude no respeta mis estándares."** | Guards **bloqueantes**: routing de modelos, saturación de RAM, comandos peligrosos, PHI, migraciones, análisis estático — cortan el error *antes* de que ocurra. |
| 💸 **"Gasto de más en modelos caros."** | Router que asigna el modelo correcto por tarea (Opus para síntesis, Sonnet para el grueso, Haiku para lo trivial) — pagas por lo que necesitas. |
| 👀 **"Un fundador no-técnico no puede saber si el trabajo avanza."** | El **cowork**: ve el build en vivo, commits reales anotados con el *porqué*. Git es la verdad; ARIS4U la explica. |
| 🔁 **"Cada sesión empieza de cero."** | El contexto, las decisiones y hasta los comentarios se reinyectan solos en la siguiente sesión. |
| 🤝 **"Quiero que un equipo colabore sobre lo que Claude construye."** | Panel compartido: el no-técnico describe, el operador aprueba, Claude construye, los devs comentan por commit. |

---

## Beneficios completos

**Memoria e inteligencia**
- 🧠 Recall híbrido (texto FTS5 + semántico con Ollama) scoped por-cliente.
- 📌 Decisiones y *guards* "locked" que Claude no contradice sin evidencia nueva.
- 🔎 Búsqueda de cualquier decisión/patrón pasado por lenguaje natural.
- ♻️ Reinyección automática de contexto y comentarios en cada nueva sesión.

**Gobernanza y seguridad**
- 🛡️ ~22 hooks en 7 eventos; guards **bloqueantes** (model routing, RAM, bash, PHI, migration lint, análisis estático nativo).
- 📊 `amplification_score` — el "FICO score" de tu trabajo amplificado por IA, medido por señales reales.
- 🔗 Trazabilidad auditable (hash-chain opcional) de cada acción.
- 🏥 Modo PHI/HIPAA opt-in para datos clínicos (off por defecto).

**Orquestación y costo**
- 🎚️ Routing de modelos por tarea → ahorro de costo con calidad.
- 🤖 Orquestación multi-agente con gates de cierre (auditor read-only distinto del autor).
- 🧰 7 MCP tools: `aris_search`, `aris_ingest`, `aris_recall_client`, `aris_dialectic`, `aris_health`, `aris_structure`, `aris_critique`.

**El producto: cowork para devs**
- 🖥️ Live Console local: memoria, telemetría, hooks, routing y el panel de proyecto en tu navegador.
- 🔨 Panel "Proyecto": el no-técnico ve el build en vivo (commits anotados con el porqué) y comenta por SHA.
- 📥 Intake: describe + sube docs → un build real se dispara y aparece en el panel.
- 🔒 Anti-Goodhart por diseño: **git es el ancla**, ARIS4U solo explica — el progreso nunca se mide desde señales gameables.

---

## ¿Para qué es cada cosa?

| Componente | Qué hace | Para qué te sirve |
|---|---|---|
| **Hooks / guards** | Interceptan cada acción de Claude | Hacen cumplir tus reglas automáticamente, sin que tú vigiles |
| **Memoria (`sessions.db`)** | Guarda decisiones/guards/digests por cliente | Claude nunca "olvida" el contexto de tu proyecto |
| **MCP tools** | Búsqueda, ingesta y review desde Claude | Consultas y guardas conocimiento sin salir de la conversación |
| **Live Console** (`console/`) | Panel web local en `:8787` | Ves el estado real del sistema y el build en vivo |
| **Cowork / Proyecto** | Intake → build → panel → comentarios | Un no-técnico opera a Claude+devs como un producto |
| **Model router** | Elige el modelo por tarea | Bajas costo sin perder calidad |
| **Skills** (`/aris-*`) | Onboarding, config, status, council | Instalas, configuras y auditas ARIS4U en lenguaje natural |

---

## Instalación (elige tu perfil)

> Guía visual completa con botones de copia: abre **`ARIS4U-INSTALL.html`** (consola de instalación por tipo de usuario).

### 🧑‍💻 Developer (instalación estándar)
```bash
git clone https://github.com/aris4u-dev/aris4u ~/projects/aris4u
cd ~/projects/aris4u && bash install.sh
claude plugin marketplace add ~/projects/aris4u
claude plugin install aris4u
```
Luego, en una sesión de Claude Code:
```
/aris-onboard      # te muestra las 7 env vars para pegar en settings.json
/aris-status       # verifica hooks + MCP + guards + memoria
```
**Requisitos:** Python ≥ 3.11, `jq`. Ollama es opcional (sin él, recall FTS5 en vez de semántico).

### 🙋 Cliente / CEO (no instala nada)
No instalas el plugin: alguien corre la instancia (`python -m aris4u_console.server`) y tú entras por el navegador a `http://127.0.0.1:8787`, pestaña **📥 Nuevo proyecto**. Describe lo que quieres, y en **🔨 Proyecto** ves el build en vivo.

### 🏥 Healthcare / PHI
Igual que Developer + activa el modo clínico:
```bash
# en settings.json:  "ARIS4U_HEALTHCARE": "1"
/aris-config --healthcare on
```
Los datos PHI viven solo en servidores del cliente, nunca en logs ni APIs externas.

---

## Verificar y desarrollar

```bash
pip install -e ".[dev]"                      # deps de desarrollo
pytest -m "not integration and not livehost" # la suite
python tools/adapt/smoke_test.py             # smoke (degrada limpio sin Ollama)
```

---

## Filosofía

- **Git es la verdad.** ARIS4U explica el *porqué*; nunca juzga el progreso desde señales propias (evita Goodhart por diseño).
- **La cognición se renta.** ARIS4U no compite con Claude — potencia el canal: memoria, gobernanza, trazabilidad.
- **Instalable por terceros.** Todo el sistema se distribuye en este repo; nada depende del cluster de nadie.

---

<div align="center">

**Business Source License 1.1** (→ MIT en 2030-07-09) · Hecho para que saques la máxima ventaja de tu cuenta de Claude.

</div>
