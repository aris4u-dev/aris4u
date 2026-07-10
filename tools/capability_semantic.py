#!/usr/bin/env python3
"""Capa de matching SEMÁNTICO para el enrutador de capacidades (Fase 2).

El router por keywords (``tools/capability_router.py``) solo dispara cuando el prompt
contiene un substring literal de la lista de triggers → cobertura ~1.9%. Esta capa
añade matching por EMBEDDING: embebe el prompt con el mismo embedder local que
``engine/v16/f1_classifier`` (mxbai-embed-large vía Ollama) y lo compara por similitud
coseno contra los embeddings de las descripciones/triggers/hints de TODAS las
capacidades del inventario vivo (``tools/capability_inventory.build_live_snapshot`` /
``collect``). Selecciona top-k por encima de un umbral.

Diseño (mismas leyes que el router keyword):
  - PRECISIÓN ante todo: umbral conservador (mxbai tiene baseline alto ~0.55; los
    saludos quedan muy por debajo). Un hint equivocado entrena a ignorar los hints.
  - GATING preservado: tras el match semántico se aplica el MISMO gating del keyword
    router (anti_triggers / intent / context) cuando la capacidad tiene entrada curada
    en el catálogo; las capacidades solo-inventario (sin entrada) usan gating neutro.
  - FAIL-OPEN TOTAL: si Ollama/numpy/caché no están, ``augment()`` devuelve ``[]`` y el
    router cae EXACTO a su comportamiento keyword. Nunca lanza, nunca bloquea el hook.
  - SIN cómputo pesado en caliente: ``route()`` solo CONSUME la caché en disco
    (``data/capability_embeddings.npz`` + sidecar de records). La construcción de los
    embeddings (1 embed por capacidad, ~20s en frío) corre fuera de banda vía
    ``build()`` (CLI ``--build-index`` / mantenimiento), nunca dentro de un prompt.

Caché (per-máquina, .gitignore — son grandes/binarios y reflejan el toolkit del dueño):
  - ``data/capability_embeddings.npz``           — matriz (N×dim) + names + hash + model
  - ``data/capability_routing_records.json``     — metadata de gating/hint por capacidad
Ambos llevan el mismo ``hash`` del contenido del inventario; si difieren entre sí o del
inventario actual, la capa semántica se auto-desactiva (degrada a keyword) hasta rebuild.

Uso:
    python3 tools/capability_semantic.py --build       # (re)genera la caché de embeddings
    python3 tools/capability_semantic.py "audita esto" # prueba el match semántico
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

ARIS_ROOT = Path(__file__).resolve().parent.parent
EMB_CACHE = ARIS_ROOT / "data" / "capability_embeddings.npz"
RECORDS_SIDECAR = ARIS_ROOT / "data" / "capability_routing_records.json"
CANARY_MARKER = ARIS_ROOT / "data" / "capability_embeddings.canary_ok"

MODEL = "mxbai-embed-large"

# Tipos de capacidad que tiene sentido SUGERIR (rutables). Hooks/mcp_server/builtin_tool
# no se sugieren como acción opt-in (hooks son automáticos; servers son contenedores).
_ROUTABLE_CTYPES = frozenset({"skill", "command", "agent", "mcp_tool"})

# Umbral coseno por defecto. mxbai-embed-large tiene baseline alto (~0.55 para texto no
# relacionado); los matches reales rondan 0.68-0.72. 0.70 prioriza PRECISIÓN: descarta el
# ruido de borde (~0.66-0.69, falsos positivos medidos: supabase.list_tables/backup-verify)
# a costa de algún match real débil. Decisión del usuario 2026-06-30 (precision-first del
# catálogo). Bajar a 0.66 si se prioriza recall. Override: ARIS4U_ROUTER_SEM_THRESHOLD.
_DEFAULT_THRESHOLD = 0.70
# A partir de esta similitud consideramos el match de ALTA confianza → el router puede
# subir su tope de hints de 2 a ~4. Override: ARIS4U_ROUTER_SEM_HIGH.
_DEFAULT_HIGH_CONF = 0.72


# --------------------------------------------------------------------------- #
# Embedder local (mismo mecanismo que engine/v16/f1_classifier._embed_text)
# --------------------------------------------------------------------------- #
def _ollama_url() -> str:
    """URL de Ollama Mac; reusa la config del engine, con fallback fail-open."""
    try:
        from engine.v16.config import OLLAMA_MAC_URL

        return OLLAMA_MAC_URL
    except Exception:
        return os.environ.get("ARIS4U_OLLAMA_MAC_URL", "http://localhost:11434")


def _np() -> Any:
    """Importa numpy de forma fail-open; None si no está disponible."""
    try:
        import numpy as np

        return np
    except Exception:
        return None


def embed_text(
    text: str,
    url: str | None = None,
    model: str = MODEL,
    timeout: int = 10,
) -> Optional[list[float]]:
    """Embebe un texto vía Ollama (idéntico a f1_classifier._embed_text).

    Args:
        text: Texto a embeber (se trunca a 2000 chars).
        url: Base URL de Ollama; por defecto la de la config del engine.
        model: Modelo de embeddings (mxbai-embed-large, el de f1_classifier).
        timeout: Segundos máximos para la llamada a Ollama.

    Returns:
        Vector de embedding como lista de floats, o None si falla (fail-open).
    """
    base = url or _ollama_url()
    try:
        result = subprocess.run(
            [
                "curl",
                "-s",
                f"{base}/api/embeddings",
                "-d",
                json.dumps({"model": model, "prompt": text[:2000]}),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        data = json.loads(result.stdout)
        emb = data.get("embedding")
        return emb if emb else None
    except (
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        KeyError,
        FileNotFoundError,
        OSError,
    ):
        return None


# --------------------------------------------------------------------------- #
# Gating (réplica mínima del router keyword — decoplado para evitar import circular)
# --------------------------------------------------------------------------- #
def _intent_ok(entry_intents: list[str], intent: str) -> bool:
    """La intención del prompt casa con la de la capacidad (o no descalifica)."""
    if not entry_intents or "any" in entry_intents:
        return True
    if not intent:
        return True
    return intent in entry_intents


def _context_ok(ctx: str, cwd: str) -> bool:
    """El contexto requerido (substrings de cwd separados por '|') se cumple o no aplica."""
    if not ctx:
        return True
    cwd_lc = (cwd or "").lower()
    return any(p.strip() and p.strip() in cwd_lc for p in ctx.lower().split("|"))


# --------------------------------------------------------------------------- #
# Construcción de los "routing records" (doc a embeber + metadata de gating/hint)
# --------------------------------------------------------------------------- #
def _synth_hint(name: str, ctype: str, description: str, invocation: str) -> str:
    """Sintetiza un hint para una capacidad solo-inventario (sin entrada curada)."""
    label = {
        "skill": "skill",
        "command": "comando",
        "agent": "agente",
        "mcp_tool": "tool MCP",
    }.get(ctype, ctype)
    desc = (description or "").strip()
    if len(desc) > 140:
        desc = desc[:137].rstrip() + "..."
    out = f"💡 {name} ({label})"
    if desc:
        out += f": {desc}"
    if invocation:
        out += f" → {invocation}"
    return out


def _record_from_cap(
    cap: dict[str, Any],
    catalog_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    """Construye un routing record para UNA capacidad (curada o solo-inventario).

    Args:
        cap: Capacidad del inventario (``{name, ctype, description, invocation, ...}``).
        catalog_entry: Entrada curada del catálogo con el mismo nombre, o None.

    Returns:
        Record ``{name, ctype, doc, intent, anti_triggers, context, hint, confidence,
        invocation}``. Hereda gating/hint del catálogo si existe; si no, gating neutro.
    """
    name = cap["name"]
    ctype = cap.get("ctype", "")
    desc = (cap.get("description") or "").strip()
    invocation = cap.get("invocation") or ""
    doc = f"{name}. {desc}".strip()
    if catalog_entry:
        intent = list(catalog_entry.get("intent") or ["any"])
        anti = list(catalog_entry.get("anti_triggers") or [])
        context = catalog_entry.get("context") or ""
        hint = catalog_entry.get("hint") or _synth_hint(name, ctype, desc, invocation)
        confidence = catalog_entry.get("confidence", "med")
        triggers = " ".join(catalog_entry.get("triggers") or [])
        if triggers:
            doc = f"{doc} {triggers}".strip()
    else:
        intent, anti, context = ["any"], [], ""
        hint = _synth_hint(name, ctype, desc, invocation)
        confidence = "med"
    return {
        "name": name,
        "ctype": ctype,
        "doc": doc,
        "intent": intent,
        "anti_triggers": anti,
        "context": context,
        "hint": hint,
        "confidence": confidence,
        "invocation": invocation,
    }


def build_routing_records(
    catalog: list[dict[str, Any]],
    inventory_caps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Funde catálogo curado + inventario vivo en una lista de records rutables.

    Para cada capacidad rutable del inventario produce un record con ``doc`` (texto a
    embeber) + metadata de gating/hint (ver ``_record_from_cap``). Las entradas de tipo
    ``flow`` se excluyen (son del router keyword, precisión crítica — la capa semántica
    nunca fabrica flujos).

    Args:
        catalog: Catálogo curado (``data/capability_triggers.json``).
        inventory_caps: Lista de capacidades del inventario vivo (``collect()``).

    Returns:
        Lista de records ordenada de forma determinista por nombre (la matriz de
        embeddings se alinea fila-a-fila con este orden).
    """
    cat_by_name = {
        e["name"]: e
        for e in catalog
        if e.get("name") and e.get("ctype") != "flow"
    }
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cap in inventory_caps:
        name = cap.get("name")
        if cap.get("ctype", "") not in _ROUTABLE_CTYPES or not name or name in seen:
            continue
        seen.add(name)
        records.append(_record_from_cap(cap, cat_by_name.get(name)))
    records.sort(key=lambda r: r["name"])
    return records


def _docs_hash(records: list[dict[str, Any]]) -> str:
    """Hash determinista del contenido a embeber (invalida la caché si cambia el inventario)."""
    h = hashlib.sha256()
    for r in records:
        h.update(r["name"].encode("utf-8"))
        h.update(b"\x00")
        h.update(r["doc"].encode("utf-8"))
        h.update(b"\x01")
    h.update(MODEL.encode("utf-8"))
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Persistencia de la caché (npz de embeddings + sidecar JSON de metadata)
# --------------------------------------------------------------------------- #
_SIDECAR_FIELDS = (
    "name",
    "ctype",
    "intent",
    "anti_triggers",
    "context",
    "hint",
    "confidence",
    "invocation",
)


def _save_index(matrix: Any, records: list[dict[str, Any]], docs_hash: str) -> bool:
    """Persiste matriz de embeddings + sidecar de metadata. False ante cualquier fallo.

    El npz solo guarda datos numéricos/escalares (matriz float32 + hash/model como str)
    para poder cargarse SIN ``allow_pickle`` (no se deserializa código). Los nombres se
    derivan del orden de records del sidecar (la fila i de la matriz = record i).
    """
    np = _np()
    if np is None:
        return False
    try:
        EMB_CACHE.parent.mkdir(parents=True, exist_ok=True)
        # hash/model como arrays de str (0-d) → loadable sin pickle.
        np.savez(EMB_CACHE, matrix=matrix, hash=docs_hash, model=MODEL)
        slim = [{k: r[k] for k in _SIDECAR_FIELDS} for r in records]
        RECORDS_SIDECAR.write_text(
            json.dumps(
                {"hash": docs_hash, "model": MODEL, "records": slim},
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        try:
            CANARY_MARKER.touch()
        except OSError:
            pass
        return True
    except Exception:
        return False


def load_index() -> Optional[dict[str, Any]]:
    """Carga la matriz de embeddings cacheada. None si falta/corrupta/modelo distinto.

    Carga SIN ``allow_pickle`` (el npz solo lleva floats + strings): no se deserializa
    ningún objeto Python arbitrario. Los nombres de capacidad se obtienen del sidecar.
    """
    np = _np()
    if np is None or not EMB_CACHE.exists():
        return None
    try:
        d = np.load(EMB_CACHE, allow_pickle=False)
        if str(d["model"]) != MODEL:
            return None
        return {"matrix": d["matrix"], "hash": str(d["hash"])}
    except Exception:
        return None


def _load_sidecar() -> Optional[dict[str, Any]]:
    """Carga el sidecar de metadata de records. None si falta/corrupto."""
    try:
        data = json.loads(RECORDS_SIDECAR.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        return None
    return data


def _threshold(override: float | None = None) -> float:
    """Umbral coseno efectivo (param > env > default), fail-open al default."""
    if override is not None:
        return override
    try:
        return float(os.environ.get("ARIS4U_ROUTER_SEM_THRESHOLD", _DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD


def high_conf_threshold() -> float:
    """Similitud a partir de la cual el match es de ALTA confianza (sube el tope de hints)."""
    try:
        return float(os.environ.get("ARIS4U_ROUTER_SEM_HIGH", _DEFAULT_HIGH_CONF))
    except (TypeError, ValueError):
        return _DEFAULT_HIGH_CONF


# --------------------------------------------------------------------------- #
# Match semántico en caliente (consume la caché; NUNCA construye)
# --------------------------------------------------------------------------- #
def augment(
    prompt: str,
    intent: str = "",
    cwd: str = "",
    exclude_names: Any = frozenset(),
    top_k: int = 4,
    threshold: float | None = None,
) -> list[dict[str, Any]]:
    """Devuelve hints semánticos para *prompt* (FAIL-OPEN: ``[]`` ante cualquier problema).

    Embebe el prompt y lo compara contra la matriz cacheada; aplica el gating curado
    (anti_triggers/intent/context) a cada candidato sobre el umbral. Cualquier excepción,
    Ollama caído, caché ausente o desincronizada → ``[]`` (el router degrada a keyword).

    Args:
        prompt: Texto del usuario.
        intent: Intención clasificada (gating).
        cwd: Directorio de trabajo (gating de contexto cliente/proyecto).
        exclude_names: Nombres ya cubiertos por el keyword router (se omiten — dedup).
        top_k: Máximo de hints semánticos a devolver.
        threshold: Umbral coseno (override del default/env).

    Returns:
        Lista de ``{name, hint, confidence, score, flow=None, via='semantic', sim}``
        ordenada por similitud descendente.
    """
    try:
        return _augment(prompt, intent, cwd, frozenset(exclude_names), top_k, threshold)
    except Exception:
        return []


def _passes_gating(
    record: dict[str, Any],
    prompt_lc: str,
    intent: str,
    cwd: str,
) -> bool:
    """¿El candidato semántico pasa el MISMO gating del keyword router?"""
    if any(a and a.lower() in prompt_lc for a in (record.get("anti_triggers") or [])):
        return False
    if not _intent_ok(record.get("intent") or [], intent):
        return False
    return _context_ok(record.get("context") or "", cwd)


def _similarities(prompt: str, matrix: Any, n_rows: int, np: Any) -> Optional[Any]:
    """Embebe el prompt y devuelve el vector de similitudes coseno (o None fail-open)."""
    if getattr(matrix, "size", 0) == 0:
        return None
    emb = embed_text(prompt)
    if emb is None:
        return None
    vec = np.asarray(emb, dtype=np.float32)
    nrm = float(np.linalg.norm(vec))
    if nrm == 0.0:
        return None
    vec = vec / nrm
    mat = np.asarray(matrix, dtype=np.float32)
    if mat.ndim != 2 or mat.shape[0] != n_rows:
        return None
    mat_n = mat / np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9, None)
    return mat_n @ vec


def _select_hints(
    sims: Any,
    records: list[dict[str, Any]],
    prompt: str,
    intent: str,
    cwd: str,
    exclude_names: frozenset,
    thr: float,
    top_k: int,
    np: Any,
) -> list[dict[str, Any]]:
    """Recorre los candidatos por similitud descendente, aplica gating y arma los hints."""
    prompt_lc = prompt.lower()
    out: list[dict[str, Any]] = []
    for i in np.argsort(-sims):
        s = float(sims[int(i)])
        if s < thr:
            break
        record = records[int(i)]
        name = record.get("name")
        if name in exclude_names or not _passes_gating(record, prompt_lc, intent, cwd):
            continue
        out.append(
            {
                "name": name,
                "hint": record.get("hint", ""),
                "confidence": record.get("confidence", "med"),
                "score": round(s, 3),
                "flow": None,
                "via": "semantic",
                "sim": round(s, 3),
            }
        )
        if len(out) >= top_k:
            break
    return out


def _augment(
    prompt: str,
    intent: str,
    cwd: str,
    exclude_names: frozenset,
    top_k: int,
    threshold: float | None,
) -> list[dict[str, Any]]:
    """Núcleo de ``augment`` (puede lanzar; ``augment`` lo envuelve fail-open)."""
    np = _np()
    if np is None or not prompt or not prompt.strip():
        return []
    sidecar = _load_sidecar()
    idx = load_index()
    if not sidecar or idx is None or idx["hash"] != sidecar.get("hash"):
        return []  # ausente o desincronizada (inventario cambió) → degradar a keyword
    records = sidecar["records"]
    sims = _similarities(prompt, idx["matrix"], len(records), np)
    if sims is None:
        return []
    return _select_hints(
        sims, records, prompt, intent, cwd, exclude_names, _threshold(threshold), top_k, np
    )


# --------------------------------------------------------------------------- #
# Construcción de la caché (fuera de banda — CLI / mantenimiento, NO en caliente)
# --------------------------------------------------------------------------- #
def build(force: bool = False) -> dict[str, Any]:
    """(Re)genera la caché de embeddings desde el inventario vivo + catálogo curado.

    Idempotente: si el hash del inventario no cambió y ``force`` es False, no re-embebe.
    Si Ollama no responde a mitad, aborta SIN tocar la caché previa (no la corrompe).

    Args:
        force: Re-embeber aunque el hash no haya cambiado.

    Returns:
        Dict de estado: ``{rebuilt, hash, count}`` o ``{rebuilt: False, error}``.
    """
    np = _np()
    if np is None:
        return {"rebuilt": False, "error": "numpy no disponible"}
    try:
        from tools import capability_inventory as ci
        from tools.capability_router import load_catalog
    except Exception as e:  # noqa: BLE001 — reportado, no silencioso
        return {"rebuilt": False, "error": f"import: {type(e).__name__}: {e}"}

    inventory = ci.collect().get("capabilities", [])
    catalog = load_catalog()
    records = build_routing_records(catalog, inventory)
    docs_hash = _docs_hash(records)

    existing = load_index()
    if existing is not None and existing["hash"] == docs_hash and not force:
        return {"rebuilt": False, "hash": docs_hash, "count": len(records), "reason": "fresh"}

    embeddings: list[list[float]] = []
    for r in records:
        e = embed_text(r["doc"])
        if e is None:
            return {
                "rebuilt": False,
                "error": "ollama no respondió",
                "embedded": len(embeddings),
                "total": len(records),
            }
        embeddings.append(e)
    matrix = np.array(embeddings, dtype=np.float32)
    if not _save_index(matrix, records, docs_hash):
        return {"rebuilt": False, "error": "fallo al persistir"}
    return {"rebuilt": True, "hash": docs_hash, "count": len(records)}


def main(argv: list[str]) -> int:
    """CLI: ``--build`` (re)genera la caché; un prompt suelto prueba el match."""
    if "--build" in argv or "--build-index" in argv:
        res = build(force="--force" in argv)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("rebuilt") or res.get("reason") == "fresh" else 1
    words = [a for a in argv if not a.startswith("--")]
    if not words:
        print("uso: capability_semantic.py --build | \"<prompt>\"")
        return 0
    hints = augment(" ".join(words))
    print(json.dumps(hints, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(ARIS_ROOT))
    raise SystemExit(main(sys.argv[1:]))
