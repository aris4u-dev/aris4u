#!/usr/bin/env python3
"""Tagger IDEMPOTENTE de client_id para observations de claude-mem.

Asigna client_id a observations con client_id IS NULL usando la señal de
files_read/files_modified (prioridad 1) o la columna project (prioridad 2).

La canonicalización de client_id es IDÉNTICA a hooks/write_client_bridge.sh:
  lowercase → quitar sufijo (-platform/-website/-app/-web) → alias map.

Diseñado para correr en session_start fire-and-forget; safe con el worker
async de claude-mem (timeout=30s, PRAGMA busy_timeout, batches de 200).

Uso típico:
    # Ver qué etiquetaría (sin escribir):
    python3 tools/tag_observations_client.py --dry-run
    # Etiquetar últimas 48h (default):
    python3 tools/tag_observations_client.py
    # Etiquetar TODO el histórico:
    python3 tools/tag_observations_client.py --since 0
    # Sobre una copia de seguridad:
    python3 tools/tag_observations_client.py --db /tmp/copy.db --since 0
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Canonicalización — espejo exacto de hooks/write_client_bridge.sh
# ---------------------------------------------------------------------------

# Sufijos a quitar en orden. NUNCA usar greedy split (rompe 'acme-wellness' → 'acme').
_SUFFIX_STRIP: tuple[str, ...] = ("-platform", "-website", "-app", "-web")

# Proyectos genéricos en la columna project — no aportan señal de cliente.
_GENERIC_PROJECTS: frozenset[str] = frozenset({"observer-sessions", Path.home().name})


def _load_alias_from_config() -> dict[str, str]:
    """Lee client_aliases de ~/.aris4u/config.json (o ARIS4U_CONFIG override).

    Devuelve {} si el archivo no existe o no contiene la clave.
    No lanza nunca: config ausente => genérico (solo /projects/03-clients/<X> da señal).
    """
    try:
        cfg_env = os.environ.get("ARIS4U_CONFIG", "").strip()
        cfg_path = Path(cfg_env) if cfg_env else Path.home() / ".aris4u" / "config.json"
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            aliases = data.get("client_aliases", {})
            if isinstance(aliases, dict):
                return {str(k).lower(): str(v) for k, v in aliases.items()}
    except Exception:  # noqa: BLE001
        pass
    return {}


# Alias: raw name (tras suffix-strip, en lowercase) → canonical client_id.
# Cargados desde ~/.aris4u/config.json campo "client_aliases".
# Config ausente ⇒ {} ⇒ solo /projects/03-clients/<X> da señal de cliente.
_ALIAS: dict[str, str] = _load_alias_from_config()

# ---------------------------------------------------------------------------
# Patrones de extracción de rutas
# ---------------------------------------------------------------------------

# 1. Patrón canónico write_client_bridge.sh: /projects/03-clients/<X>
_RE_03_CLIENTS: re.Pattern[str] = re.compile(r"/projects/03-clients/([^/]+)")


def _rebuild_patterns(
    alias: dict[str, str],
) -> tuple[list[str], Optional[re.Pattern[str]]]:
    """Compila _KNOWN_NAMES y _RE_BROAD desde un alias-dict dado.

    Factorizado para ser reutilizable por fixtures de tests sin necesidad de
    depender de ~/.aris4u/config.json.

    Args:
        alias: Mapa {raw_name_lowercase → canonical_client_id}.

    Returns:
        Tupla (known_names, re_broad).
    """
    known_names: list[str] = sorted(alias.keys(), key=len, reverse=True)
    re_broad: Optional[re.Pattern[str]] = (
        re.compile(r"/(" + "|".join(re.escape(n) for n in known_names) + r")(?:/|$)")
        if known_names
        else None
    )
    return known_names, re_broad


# 2. Barrido amplio: nombres conocidos como segmento de ruta (longest-first).
#    None cuando _ALIAS vacío (config ausente) → omitido en _collect_candidate_from_path.
_KNOWN_NAMES, _RE_BROAD = _rebuild_patterns(_ALIAS)


# ---------------------------------------------------------------------------
# Funciones puras (testeables sin DB)
# ---------------------------------------------------------------------------


def canonicalize_client(raw: str) -> Optional[str]:
    """Canonicaliza un nombre raw de proyecto/carpeta a client_id.

    Espejo exacto de hooks/write_client_bridge.sh:
      1. lowercase
      2. quitar sufijo conocido (-platform/-website/-app/-web), solo el primero
      3. lookup en alias map → None si no es un cliente conocido

    Args:
        raw: Nombre de proyecto extraído de un path o de la columna project.

    Returns:
        client_id canónico (str) o None.
    """
    if not raw:
        return None
    name = raw.lower()
    for suf in _SUFFIX_STRIP:
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    return _ALIAS.get(name)


def _extract_paths(json_field: Optional[str]) -> list[str]:
    """Parsea un array JSON de paths desde una columna nullable de SQLite.

    Tolerante: JSON malformado → lista vacía (no aborta el barrido).

    Args:
        json_field: String JSON raw desde files_read o files_modified.

    Returns:
        Lista de path strings; vacía si el campo es null/vacío/malformado.
    """
    if not json_field or json_field in ("null", "[]", ""):
        return []
    try:
        parsed = json.loads(json_field)
        if isinstance(parsed, list):
            return [str(p) for p in parsed if p]
        return []
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def _resolve_candidates(candidates: set[str]) -> Optional[str]:
    """Resuelve colisiones entre múltiples client_id candidatos.

    Prefiere no-aris4u. Si siguen 2+ no-aris4u → None (ambiguo).

    Args:
        candidates: Set de client_id candidatos para una observation.

    Returns:
        client_id resuelto o None.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return next(iter(candidates))
    non_aris4u = candidates - {"aris4u"}
    if not non_aris4u:
        return "aris4u"
    if len(non_aris4u) == 1:
        return next(iter(non_aris4u))
    return None  # 2+ clientes no-aris4u → no adivinar


def infer_client_from_paths(
    files_read_json: Optional[str],
    files_modified_json: Optional[str],
) -> Optional[str]:
    """Infiere client_id desde los paths de files_read y files_modified.

    Por cada path prueba en orden:
      1. /projects/03-clients/<X>/  (canónico, igual que write_client_bridge.sh)
      2. Barrido amplio: nombre conocido como segmento de ruta
         (requiere client_aliases en ~/.aris4u/config.json; omitido si config ausente).

    En colisión multi-cliente delega a _resolve_candidates.

    Args:
        files_read_json: JSON array string de la columna files_read.
        files_modified_json: JSON array string de la columna files_modified.

    Returns:
        client_id canónico o None.
    """
    all_paths = _extract_paths(files_read_json) + _extract_paths(files_modified_json)
    if not all_paths:
        return None

    candidates: set[str] = set()
    for path in all_paths:
        _collect_candidate_from_path(path, candidates)

    return _resolve_candidates(candidates)


def _collect_candidate_from_path(path: str, candidates: set[str]) -> None:
    """Extrae client_id candidato de un solo path y lo añade al set.

    Prueba los tres patrones en orden de prioridad; para en el primero que
    coincida para no contar el mismo path dos veces.

    Args:
        path: Ruta de archivo (string).
        candidates: Set mutable al que se añade el candidato inferido.
    """
    # 1. /projects/03-clients/<X>
    m = _RE_03_CLIENTS.search(path)
    if m:
        client = canonicalize_client(m.group(1))
        if client:
            candidates.add(client)
        return

    # 2. Barrido amplio: nombre conocido como segmento de ruta.
    #    _RE_BROAD es None cuando config ausente → skip silencioso.
    if _RE_BROAD is not None:
        m2 = _RE_BROAD.search(path)
        if m2:
            client = canonicalize_client(m2.group(1))
            if client:
                candidates.add(client)


def infer_client(observation_row: dict) -> Optional[str]:
    """Infiere client_id para una fila de observation.

    Prioridad 1: files_read + files_modified.
    Prioridad 2: columna project (fallback si paths no dan señal).

    Args:
        observation_row: Dict con claves files_read, files_modified, project.

    Returns:
        client_id canónico o None.
    """
    client = infer_client_from_paths(
        observation_row.get("files_read"),
        observation_row.get("files_modified"),
    )
    if client is not None:
        return client

    project = (observation_row.get("project") or "").strip()
    if project.lower().startswith("aris4u"):
        # Runs de test del propio motor (aris4u_ecommerce, aris4u_dialectic_test, …):
        # señal inequívoca → 'aris4u' (decisión Tramo 3 §9, 2026-07-01). NO va en
        # canonicalize_client porque ese es espejo exacto de write_client_bridge.sh.
        return "aris4u"
    if project and project not in _GENERIC_PROJECTS:
        return canonicalize_client(project)
    return None


# ---------------------------------------------------------------------------
# Helpers de la capa DB (extraídos para mantener CC de main bajo)
# ---------------------------------------------------------------------------


def _fetch_candidates(
    conn: sqlite3.Connection,
    since_hours: float,
    limit: int,
) -> list[sqlite3.Row]:
    """Fetch observations con client_id NULL/vacío dentro del rango temporal.

    Args:
        conn: Conexión SQLite abierta.
        since_hours: Solo filas con created_at_epoch >= now - N horas (0 = todas).
        limit: Máx filas a devolver (0 = ilimitado).

    Returns:
        Lista de sqlite3.Row.
    """
    where_clauses = ["(client_id IS NULL OR client_id = '')"]
    params: list[object] = []
    if since_hours > 0:
        since_epoch = int(time.time() - since_hours * 3600)
        where_clauses.append("created_at_epoch >= ?")
        params.append(since_epoch)

    where_sql = " AND ".join(where_clauses)
    query = (
        f"SELECT id, files_read, files_modified, project "
        f"FROM observations WHERE {where_sql} ORDER BY id"
    )
    if limit > 0:
        query += f" LIMIT {limit}"

    return conn.execute(query, params).fetchall()


def _run_inference(
    rows: list[sqlite3.Row],
) -> tuple[dict[str, int], list[tuple[str, int]], int]:
    """Ejecuta inferencia de client_id sobre las filas candidatas.

    Args:
        rows: Filas de sqlite3.Row con id, files_read, files_modified, project.

    Returns:
        Tupla (tagged_by_client, updates, no_signal_count).
    """
    tagged_by_client: dict[str, int] = defaultdict(int)
    updates: list[tuple[str, int]] = []
    no_signal = 0

    for row in rows:
        try:
            client = infer_client(dict(row))
            if client:
                tagged_by_client[client] += 1
                updates.append((client, row["id"]))
            else:
                no_signal += 1
        except Exception as exc:  # noqa: BLE001 — fila malformada no aborta el barrido
            log.warning("Fila id=%s: error inesperado %s", row["id"], exc)
            no_signal += 1

    return dict(tagged_by_client), updates, no_signal


def _write_batches(
    conn: sqlite3.Connection,
    updates: list[tuple[str, int]],
    batch_size: int = 200,
) -> int:
    """Escribe updates en batches con commit por batch.

    Idempotente: solo toca filas donde client_id IS NULL o ''.

    Args:
        conn: Conexión SQLite abierta.
        updates: Lista de (client_id, observation_id).
        batch_size: Tamaño de cada batch (default 200).

    Returns:
        Número total de filas escritas.
    """
    written = 0
    for i in range(0, len(updates), batch_size):
        batch = updates[i : i + batch_size]
        conn.executemany(
            "UPDATE observations SET client_id = ? "
            "WHERE id = ? AND (client_id IS NULL OR client_id = '')",
            batch,
        )
        conn.commit()
        written += len(batch)
        log.info("Batch commit: %d/%d", written, len(updates))
    return written


def _propagate_to_vectors(
    obs_conn: sqlite3.Connection,
    vectors_db: Path,
) -> int:
    """Sincroniza client_id de observations → vectores en aris_vectors.db.

    El recall KNN filtra por vec_items.client_id (no por observations); sin esta
    propagación, etiquetar observations NO mejora el recall per-cliente. Lee el
    client_id YA asignado en observations (cubre lo etiquetado en este run Y en
    runs previos) y lo aplica a vec_map.client_id + vec_items.client_id (tabla vec0)
    reusando el embedding existente — NO re-embebe. Idempotente: solo toca vectores
    con client_id vacío cuya observation SÍ tiene client_id.

    Args:
        obs_conn: Conexión a claude-mem.db (para leer observations.client_id).
        vectors_db: Ruta a aris_vectors.db.

    Returns:
        Número de vectores actualizados (0 si la DB o la extensión no están).
    """
    if not vectors_db.exists():
        return 0
    try:
        import sqlite_vec  # type: ignore
    except Exception:
        log.warning("sqlite_vec no disponible — vectores NO propagados")
        return 0
    by_source_id = {
        str(r["id"]): r["client_id"]
        for r in obs_conn.execute(
            "SELECT id, client_id FROM observations "
            "WHERE client_id IS NOT NULL AND client_id != ''"
        ).fetchall()
    }
    if not by_source_id:
        return 0
    updated = 0
    try:
        con = sqlite3.connect(str(vectors_db), timeout=30)
        con.execute("PRAGMA busy_timeout = 30000")
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT rowid, source_id FROM vec_map "
            "WHERE source = 'observations' AND (client_id IS NULL OR client_id = '')"
        ).fetchall()
        for r in rows:
            client_id = by_source_id.get(str(r["source_id"]))
            if not client_id:
                continue
            con.execute(
                "UPDATE vec_items SET client_id = ? WHERE rowid = ?",
                (client_id, r["rowid"]),
            )
            con.execute(
                "UPDATE vec_map SET client_id = ? WHERE rowid = ?",
                (client_id, r["rowid"]),
            )
            updated += 1
        con.commit()
        con.close()
    except Exception as exc:
        log.warning("Propagación a vectores falló: %s", exc)
    return updated


def _print_summary(
    total_candidates: int,
    tagged_by_client: dict[str, int],
    no_signal: int,
    dry_run: bool,
) -> None:
    """Imprime resumen de resultados.

    Args:
        total_candidates: Total de filas candidatas procesadas.
        tagged_by_client: Dict {client_id: count}.
        no_signal: Filas sin señal (quedan NULL).
        dry_run: True si no se escribió en la DB.
    """
    total_tagged = sum(tagged_by_client.values())
    mode = "DRY-RUN" if dry_run else "RESULTADOS"
    print(f"\n{mode}")
    print(f"  Candidatas (client_id NULL): {total_candidates}")
    print(f"  {'Etiquetaría' if dry_run else 'Etiquetadas'}:  {total_tagged}")
    print(f"  Sin señal (quedan NULL):     {no_signal}")
    print("\n  Por cliente:")
    for client, count in sorted(tagged_by_client.items(), key=lambda x: -x[1]):
        print(f"    {client:<22} {count:>6}")


# ---------------------------------------------------------------------------
# Entrypoint CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI principal: etiqueta observations con client_id inferido."""
    parser = argparse.ArgumentParser(
        description="Tag observations con client_id inferido (idempotente).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        default=str(Path.home() / ".claude-mem" / "claude-mem.db"),
        help="Ruta a claude-mem.db (default: ~/.claude-mem/claude-mem.db)",
    )
    parser.add_argument(
        "--since",
        type=float,
        default=48.0,
        metavar="HOURS",
        help="Solo procesar observations de las últimas N horas (0 = todas; default: 48)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostrar cuántas etiquetaría por cliente SIN escribir en la DB",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Máx filas a procesar (0 = ilimitado)",
    )
    parser.add_argument(
        "--vectors-db",
        default=str(Path(__file__).resolve().parents[1] / "data" / "aris_vectors.db"),
        help="Ruta a aris_vectors.db para propagar client_id al recall (default: repo/data)",
    )
    parser.add_argument(
        "--no-vectors",
        action="store_true",
        help="No propagar a aris_vectors.db (solo etiquetar observations)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("DB no encontrada: %s", db_path)
        sys.exit(1)

    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
    except sqlite3.OperationalError as exc:
        log.error("No se puede abrir la DB: %s", exc)
        sys.exit(1)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")

    rows = _fetch_candidates(conn, args.since, args.limit)
    log.info("Filas candidatas (client_id NULL): %d", len(rows))

    tagged_by_client, updates, no_signal = _run_inference(rows)
    _print_summary(len(rows), tagged_by_client, no_signal, args.dry_run)

    if not args.dry_run:
        written = _write_batches(conn, updates)
        log.info("Listo. %d observations etiquetadas.", written)
        if not args.no_vectors:
            propagated = _propagate_to_vectors(conn, Path(args.vectors_db))
            log.info("Vectores propagados (recall per-cliente): %d", propagated)

    conn.close()


if __name__ == "__main__":
    main()
