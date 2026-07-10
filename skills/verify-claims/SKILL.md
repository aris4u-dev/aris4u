---
name: verify-claims
description: >
  Verificación adversarial de los claims técnicos de la última respuesta (propia o de
  otro agente) ANTES de darlos por buenos. Extrae cada afirmación verificable —caller,
  count, path, hash, resultado de test, "X llama a Y", "existe Z"— y la confronta con
  evidencia bash/grep/SQL viva en el mismo turno; marca cada una ✅ verificada,
  ❌ falsa, o ⚠️ no-verificable. Materializa la Rule 0.1 (CLAIM-VERIFY) y 0.2 (N de
  TOTAL). Use when: (1) "verifica eso / ¿es cierto?", (2) tras un entregable o reporte
  de un subagente, (3) antes de comprometer una decisión basada en claims, (4) cuando
  una respuesta afirma counts/paths/integraciones sin haberlos grepeado.
---

# /verify-claims — Verificación adversarial de afirmaciones

Convierte la regla más cara cuando falla (0.1 CLAIM-VERIFY, costó ~20% de uso semanal)
en un paso ejecutable. Default adversarial: **una afirmación sin evidencia fresca en
el mismo turno = no verificada**, no "probablemente cierta".

## Procedimiento

1. **Extraer claims.** De la respuesta/reporte a auditar, lista cada afirmación
   verificable. Tipos: existencia (`existe el archivo/función/tabla`), cantidad
   (`hay N callers / N tests / N filas`), relación (`A llama a B`, `X importa Y`),
   identidad (hash, versión, commit), resultado (`los tests pasan`, `el build es verde`).
   Ignora opiniones y recomendaciones — solo lo falsable.

2. **Verificar cada uno con evidencia VIVA** (re-consultar, nunca citar de memoria/docs):
   - existencia/relación/cantidad → `grep -rn`, `rg`, `find`, `ls`
   - counts → el comando que los produce + `| wc -l` (reporta **N de TOTAL**, Rule 0.2)
   - DB/migraciones → `psql`/`execute_sql` real (como rol `authenticated`, no superusuario)
   - tests/build → correr el comando, no asumir
   - hash/versión → `git rev-parse`, `shasum`, `--version`

3. **Veredicto por claim:**
   - ✅ **verificada** — evidencia mostrada en este turno
   - ❌ **falsa** — la evidencia contradice el claim (¡el hallazgo más valioso!)
   - ⚠️ **no-verificable** — no se pudo comprobar → degradar a `(unverified)` explícito

## Salida

Tabla `claim | tipo | comando de evidencia | veredicto`, y al final:
**"X de N claims verificados, Y falsos, Z sin verificar"** (subset-full, Rule 0.2).
Si hay ❌, encabezar con ellos: un claim falso comprometido es el fallo que esta skill
existe para atrapar.

## Por qué

Rules 0.1 (cada claim técnico requiere evidencia bash/grep/SQL en el mismo turn o
prefijo `(unverified)`), 0.2 (N de TOTAL), 0.3 (DOCS-FRESH: re-consultar en vivo).
