---
name: mcp-audit
description: >
  Auditoría de seguridad de los MCP servers cableados a Claude Code: qué binario/script
  ejecuta cada uno (los stdio corren código LOCAL en tu máquina = superficie de riesgo
  real), de dónde viene, si está fijado a versión o usa @latest (supply-chain), y
  detecta duplicados entre config global y .mcp.json de repos. Complementa a aris-config
  (que solo ve duplicados) añadiendo la capa de seguridad/procedencia. Use when:
  (1) antes de cablear un MCP server nuevo de un tercero, (2) "¿son seguros mis MCP /
  qué ejecutan?", (3) revisar exposición tras instalar un plugin con MCP, (4) auditoría
  periódica del stack. Para escanear el CÓDIGO de un MCP de tercero usa /skill-security-scan.
---

# /mcp-audit — Auditoría de los MCP servers cableados

Mapea y evalúa cada MCP server conectado. Foco en los **stdio**: ejecutan un comando
en tu máquina con tus permisos → son la superficie de riesgo real (los connectors
remotos de claude.ai los gestiona Anthropic).

## Cómo ejecutar

```bash
bash ~/.claude/bin/mcp-audit.sh
```

Read-only. Lee `~/.claude.json` + `.mcp.json` de repos conocidos.

## Qué reporta por server

- **Comando ejecutado** — binario/script real (`npx`, `bash script.sh`, ruta a wrapper).
- **Procedencia** — local (tu script), npm (`npx -y pkg`), o remoto (url).
- **Supply-chain** — 🔴 si usa `@latest`/sin pin (se actualiza sin tu control),
  🟡 si es `npx -y` (descarga+ejecuta), 🟢 si es script local que controlas.
- **Duplicados** — mismo server en global y en repo `.mcp.json` (config drift).

## Veredicto

| Señal | Riesgo |
|-------|--------|
| script local propio (`~/.claude/bin/*.sh`, wrapper aris4u) | 🟢 bajo |
| `npx -y pkg@<versión fija>` | 🟡 medio |
| `npx -y pkg@latest` o sin pin | 🔴 alto (auto-update sin revisión) |
| binario de tercero no auditado | 🔴 → pasar por /skill-security-scan |

## Frontera

- Procedencia/supply-chain/duplicados de MCP → **esta skill**.
- Escanear el CÓDIGO de un MCP/plugin de tercero → `/skill-security-scan`.
- Duplicados de config (sin lente de seguridad) → `aris-config`.
- Liveness de Ollama/MCP → `aris_health`.
