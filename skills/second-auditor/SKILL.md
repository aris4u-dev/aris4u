---
name: second-auditor
description: >
  Gate de cierre para CUALQUIER proyecto antes de dar por terminado un entregable:
  verificacion MECANICA segun el stack (analyze/typecheck/lint/test + IDE diagnostics)
  y LUEGO un agente revisor read-only distinto del hilo autor, con veredicto GO / NO-GO.
  El codigo NO se auto-valida. Gate COMPLETO: subsume la revision de diff (no llamar
  /code-review en paralelo) y encadena /verify-claims si hay claims. Use when: (1) cerrar
  modulo/feature/fix antes de comitear, (2) "audita esto / pasa el gate / revisalo antes
  de cerrar", (3) tras un fan-out que toco codigo, (4) antes de declarar "esta listo".
---

# /second-auditor — Gate del 2º auditor independiente

Materializa la leccion de 2026-06-25 ([[feedback_ide_plus_second_auditor]]):
la disciplina manual NO basta para cazar bugs — hace falta verificacion mecanica
**continua** + un auditor **distinto del autor**. Quien escribe el codigo no se
auto-aprueba. Default: **sin evidencia mecanica fresca en este turno = NO-GO**, no
"probablemente bien".

**Mitad automática ya activa:** el hook `~/.claude/hooks/pre-commit-gate.sh` (PreToolUse
sobre `git commit`) corre el gate MECÁNICO sobre los archivos staged y BLOQUEA el commit
si hay errores (Dart `analyze`, Python `py_compile`, Shell `bash -n`; otros stacks → aviso).
Esta skill es la mitad COMPLETA y manual: añade los tests y el agente revisor independiente
que el hook no puede correr (timeout). Override del hook: `git commit --no-verify`.

Dos principios duros:
1. **Mecanico antes que juicio.** Lo que de verdad caza los bugs es `analyze`/`typecheck`/
   `lint`/`test`, no la opinion de un LLM. Corre eso PRIMERO. Si el gate mecanico falla,
   es NO-GO sin necesidad de revision humana/LLM.
2. **Autor ≠ auditor.** La revision corre en un agente SEPARADO (read-only), no en el hilo
   que produjo el cambio. Un autor revisando su propio trabajo repite sus puntos ciegos.

## Procedimiento

### 1. Delimitar el cambio
- `git diff --stat <base>` (base = rama madre o el commit previo al bloque de trabajo) para
  saber QUE se toco. Si no hay git ni sesion con cambios, el scope es el directorio/modulo
  que indique el usuario ("audita el modulo X").
- Lista archivos + lenguajes implicados; eso decide los checks mecanicos.

### 2. Gate MECÁNICO (auto-detectar stack — corre TODO lo aplicable)
Detecta por archivos presentes en la raiz / extensiones del diff y corre el verificador.
Reporta cada uno como **N de TOTAL** (Rule 0.2):

| Stack (señal) | Verificación mecánica |
|---|---|
| Flutter/Dart (`pubspec.yaml`) | `dart analyze <archivos\|dir> ` (0 errores) + `flutter test --reporter compact` |
| Python (`pyproject.toml`/`requirements.txt`) | `ruff check` + `mypy`/`pyright` si existe + `pytest -q` |
| TypeScript/Node (`package.json`/`tsconfig.json`) | `tsc --noEmit` + `eslint` + `vitest`/`jest` run |
| Java/Spring (`pom.xml`/`build.gradle`) | `mvn -q compile` + `mvn -q test` (o gradle equiv) |
| Astro/React web | `astro check`/`tsc --noEmit` + build + tests si hay |
| Swift/iOS (`*.xcodeproj`/`Package.swift`) | `xcodebuild -scheme <name> build` + `xcodebuild test` |
| Go (`go.mod`) | `go build ./...` + `go vet ./...` + `go test ./...` |
| Rust (`Cargo.toml`) | `cargo check` + `cargo test` |
| Shell (`.sh` en el diff) | `shellcheck <files>` |
| Dockerfile (en el diff) | `hadolint Dockerfile` |
| SQL/migraciones Supabase | aplicar en branch/local + `get_advisors`; nunca asumir |

- **Diagnostics en vivo del IDE**: si el IDE esta conectado, usa `getDiagnostics` (LSP) para
  traer errores/warnings que el editor ya ve — capa que corre continua, no solo al pedirla.
  Si NO esta conectado, recomienda `/ide` (y arrancar la sesion en el cwd del proyecto).
- Cualquier `error` (no info/warning de estilo) ⇒ **NO-GO**, se listan y termina aqui.

### 3. Gate de AUDITOR INDEPENDIENTE (solo si el mecanico pasa)
- Lanza un agente **read-only distinto del autor** sobre el diff: `code-review-agent` o
  `qa-agent` (NUNCA el hilo que escribio el codigo).
- Para deltas grandes, fan-out: un revisor por dimension (correctness / seguridad /
  regresion / contrato de API) en paralelo, cada uno con evidencia.
- **Modelo: Sonnet** para cada revisor; Opus solo si el cambio toca arquitectura critica
  o una decision no reversible (los subagentes heredan Opus por defecto = caro — especifica).
- Si el entregable hizo CLAIMS textuales (counts, "X llama a Y", paths), encadena
  `/verify-claims` sobre ellos.
- El auditor reporta hallazgos con severidad + evidencia; NO arregla (read-only).

### 4. Veredicto
Encabeza con **GO** o **NO-GO**:
- **NO-GO** si hay cualquier error mecanico, test rojo nuevo, o hallazgo del auditor de
  severidad alta. Lista cada bloqueador con su evidencia (comando + salida) y el fix sugerido.
- **GO** solo con: gate mecanico verde (mostrar los `N de TOTAL`), auditor sin altas,
  claims verificados. Indica explicitamente que corrio y que no (subset-full).

## Salida
```
VEREDICTO: NO-GO
Mecánico:  dart analyze test/ = 3 errores ❌ | flutter test = 953/990 (37 rojos)
Auditor:   2 hallazgos ALTA (inyección sin sanitizar en X:42; null-deref en Y:88)
Claims:    4/5 verificados, 1 falso («0 callers» → grep muestra 3)
Bloqueadores: [lista con evidencia + fix]
```
```
VEREDICTO: GO
Mecánico:  dart analyze test/ = 0 errores ✅ | flutter test = 18/18 ✅
Auditor:   0 hallazgos ALTA/MEDIA (1 BAJA: naming, no bloquea)
Claims:    5/5 verificados
Corrido: dart analyze + flutter test + code-review-agent. NO corrido: build iOS (sin cambios nativos).
```

> El auditor también puede equivocarse: verifica sus claims (un "no existe X" suele ser
> un grep de scope incompleto). Aplica /verify-claims a los hallazgos del propio auditor
> antes de tratarlos como bloqueadores. Prerequisito implícito: `/preflight` mide RECURSOS
> ANTES de lanzar el fan-out que produce el entregable; `/second-auditor` valida el
> RESULTADO después.

## Por qué
Rule 0.1 (CLAIM-VERIFY), 0.2 (N de TOTAL), 0.7 (QUALITY = verificado, no "creo que sirve").
Una sesion real demostro 665 errores + 37 fallos que la disciplina manual no atrapaba;
el gate mecanico + auditor independiente los expuso. Relacionado: [[feedback_ide_plus_second_auditor]],
[[feedback_claim_verification]], skills `verify-claims`, `code-review`, `preflight`.
