"""Recall diverso (MMR) — mejora opt-in y REVERSIBLE del recall.

Reordena los candidatos para equilibrar relevancia (parecido a la query) con
diversidad (que no repitan lo mismo entre si). Activado por env var:
    ARIS4U_DIVERSE_RECALL=1   -> activo
    (sin la var / "0")        -> INACTIVO: el recall se comporta identico a antes.

Diseno deliberado: este modulo NO importa session_manager a nivel de modulo
(evita import circular); embed_text se importa localmente dentro de reorder().
"""
import os


def enabled() -> bool:
    """True solo si el interruptor esta encendido. Off por defecto."""
    return os.getenv("ARIS4U_DIVERSE_RECALL", "0") == "1"


def pool_size(limit: int) -> int:
    """Cuantos candidatos pedir antes de diversificar (mas si esta activo)."""
    return limit * 4 if enabled() else limit


def _mmr_indices(rel: list[float], vecs: list, limit: int, lam: float) -> list[int]:
    """Selecciona indices por Maximal Marginal Relevance (relevancia vs diversidad)."""
    chosen: list[int] = []
    pool = list(range(len(rel)))
    while pool and len(chosen) < limit:
        best, best_s = pool[0], -1e9
        for c in pool:
            div = max((float(vecs[c] @ vecs[ch]) for ch in chosen), default=0.0)
            s = lam * rel[c] - (1 - lam) * div
            if s > best_s:
                best, best_s = c, s
        chosen.append(best)
        pool.remove(best)
    return chosen


def reorder(query: str, results: list[dict], limit: int, lam: float = 0.7) -> list[dict]:
    """Reordena results por MMR. Best-effort: ante cualquier fallo devuelve results[:limit]."""
    if len(results) <= limit:
        return results[:limit]
    try:
        import numpy as np
        from .session_manager import embed_text  # import local: evita ciclo

        qv = embed_text(query, role="query")
        if not qv:
            return results[:limit]
        q = np.asarray(qv, dtype=np.float32)
        q /= np.linalg.norm(q) + 1e-9
        vecs = []
        for r in results:
            v = embed_text(r.get("text", ""))
            if not v:
                return results[:limit]  # fail-safe: no arriesgar, comportamiento normal
            a = np.asarray(v, dtype=np.float32)
            vecs.append(a / (np.linalg.norm(a) + 1e-9))
        rel = [float(v @ q) for v in vecs]
        idx = _mmr_indices(rel, vecs, limit, lam)
        return [results[i] for i in idx]
    except Exception:
        return results[:limit]  # fail-safe: nunca rompe el recall
