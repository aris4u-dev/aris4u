"""ARIS4U hook dispatcher (V2.0) — un proceso python por evento de Claude Code.

Sustituye los N hooks .sh cableados por evento por un solo entrypoint (`dispatch.py`)
que resuelve el handler y aplica el contrato de salida (advisory / bloqueo). Migración
incremental evento por evento, cada uno con golden-test de equivalencia vs el .sh viejo.
"""
