---
name: backup-verify
description: >
  Verifica que los backups de un proyecto sean REALES y restaurables, no solo que
  existan: comprueba tamaño (detecta dumps vacíos), integridad del gzip, contenido SQL
  real (CREATE TABLE/INSERT) y antigüedad del último backup bueno. Nace de un incidente
  real: pg_dump generaba .sql.gz de 20 bytes (vacíos) a diario durante semanas sin
  que nada lo detectara. Use when: (1) "¿están bien mis backups / sirven para restaurar?",
  (2) tras configurar o cambiar un cron de backup, (3) auditoría periódica de un proyecto
  con datos críticos, (4) ANTES de confiar en un backup para restaurar. Read-only.
---

# /backup-verify — ¿Tus backups sirven de verdad?

"Existe un archivo de backup" ≠ "puedo restaurar". Esta skill cierra esa brecha:
un backup vacío o corrupto da falsa tranquilidad hasta el día que lo necesitas.

## Cómo ejecutar

```bash
bash ~/.claude/bin/backup-verify.sh <directorio-de-backups> [umbral-bytes]
```

Ejemplos:
```bash
bash ~/.claude/bin/backup-verify.sh ~/projects/acme-corp/data/backups
bash ~/.claude/bin/backup-verify.sh ~/projects/mi-proyecto/infra/backups 1024
```

Umbral por defecto: 1024 bytes (por debajo = casi seguro vacío).

## Qué verifica por archivo

1. **Tamaño** — < umbral → 🔴 VACÍO (un .sql.gz real pesa KB/MB; 20 bytes = gzip de nada).
2. **Integridad** — `gzip -t` para .gz / abrible para .sql.
3. **Contenido** — descomprime una muestra y busca `CREATE TABLE`/`INSERT`/`COPY`;
   sin eso → 🔴 SIN DATOS aunque el gzip sea válido.
4. **Frescura** — antigüedad del backup BUENO más reciente (no del archivo más reciente,
   que puede ser un vacío).

## Salida

Tabla `archivo · tamaño · gzip · contenido · veredicto` y resumen:
**"X de N backups válidos · último bueno hace Y días"**. Si el más reciente está
vacío/corrupto → encabeza con alerta 🔴 (el caso real que originó esta skill).

## Por qué

Incidente documentado: pg_dump generaba .sql.gz vacíos a diario durante semanas sin que
nada lo detectara. Esta skill lo habría gritado en el primer run.
