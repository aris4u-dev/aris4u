---
name: skill-security-scan
description: >
  Gate de seguridad pre-instalación para skills, plugins y MCP servers de TERCEROS,
  usando SkillSpector (NVIDIA) con la etapa de análisis semántico corriendo en un
  modelo LOCAL (Ollama) — nada de código sale del Mac. Escanea un repo/dir/zip/URL
  buscando prompt injection, exfiltración, credential harvesting, supply-chain,
  MCP tool-poisoning y ejecución peligrosa; da score 0-100 y SAFE/CAUTION/DO-NOT-INSTALL.
  Use when: (1) ANTES de instalar/cablear un plugin, skill o MCP de un tercero,
  (2) "¿es seguro instalar X?", (3) auditar un marketplace o repo ajeno, (4) revisar
  qué expone un MCP server nuevo. NO lo uses como veredicto en modo estático puro:
  esta skill SIEMPRE usa la etapa LLM local porque el estático marca hasta plugins
  first-party de Anthropic como CRÍTICO (falsos positivos).
---

# /skill-security-scan — Gate de seguridad pre-install (3ros)

Escanea código de terceros ANTES de cablearlo. Cierra el gap de supply-chain: skills,
plugins y MCP se instalan con confianza implícita (research NVIDIA: 26% con vulns,
5% intención maliciosa). Análisis semántico en **Ollama local** → privacy-safe.

## Cómo ejecutar

```bash
bash ~/.claude/bin/skillspector-scan.sh <ruta-o-URL> [modelo-ollama]
```

Ejemplos:
```bash
bash ~/.claude/bin/skillspector-scan.sh https://github.com/user/algun-plugin
bash ~/.claude/bin/skillspector-scan.sh ~/Downloads/skill-x/ Foundation-Sec-8B
bash ~/.claude/bin/skillspector-scan.sh ~/.claude/plugins/marketplaces/otro
```

El wrapper cablea SkillSpector contra Ollama (API OpenAI-compatible en :11434),
fuerza la etapa LLM y pasa `--recursive` si es un directorio multi-skill.

## Modelo

Por defecto **Foundation-Sec-8B** (especializado en seguridad, rápido en M5). Para
juicio más fino sobre intención: `qwen3.6:35b-a3b` (más lento). NUNCA un provider
remoto para código de cliente/sensible — siempre local.

## Interpretación (CRÍTICO — leer reference_skillspector_eval)

- El **estático `--no-llm` NO es veredicto**: marca `figma`, `plugin-dev`,
  `skill-creator` de Anthropic como 100/CRITICAL por confundir instrucciones normales
  de skill y descripciones de MCP tools con ataques. Por eso esta skill usa LLM.
- Patrones legítimos comunes que disparan falsos positivos: leer `~/.claude` (config
  viewers), `curl|bash` de instaladores conocidos, `ALTER/DROP TABLE` en migraciones,
  env vars en toolkits de autoría. La etapa LLM los filtra; aun así, **lee los
  hallazgos HIGH/CRITICAL a mano** antes de decidir.
- Veredicto accionable real = exfiltración a hosts desconocidos, credenciales hacia
  fuera, hidden instructions en metadata de MCP, código ofuscado que se auto-ejecuta.

## Estado / referencia

SkillSpector v2.3.5 instalado vía `uv tool`. Evaluación completa y por qué el modo
estático sobre-marca: memoria `reference_skillspector_eval`. Mapa de capacidades: §C.
