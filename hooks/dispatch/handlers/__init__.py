"""Sub-handlers de PostToolUse — portados 1:1 de los .sh del repo.

Cada módulo expone funciones puras (sin sys.exit) que el orquestador
`dispatch.events.post_tool_use` combina en una sola salida del contrato
PostToolUse. Separados del .sh viejo para poder testearlos aislados y
preservar EXACTO su contrato (en especial la mutación de redact_secrets).
"""
