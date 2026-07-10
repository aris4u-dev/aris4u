"""ARIS4U auto-adaptación a versiones de Claude (Paso 7).

Mantiene ARIS4U al día con cada release de Claude SIN cerebro cognitivo propio:
vigía de fuentes -> clasificador determinista -> (mecánico: auto-aplica con
smoke-test + rollback | semántico: PR para el usuario). La cognición se renta de
Claude headless cuando hace falta. Gate = smoke_test.py (contrato del harness).
"""
