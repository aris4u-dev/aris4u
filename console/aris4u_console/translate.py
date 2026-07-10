"""Traducción EN→ES cacheada en disco vía Ollama local (para la consola, todo en español).

Las descripciones de capacidades de terceros (agents/skills de Claude/plugins) vienen en
inglés. Aquí se traducen con un modelo local (sin coste de API) y se cachean por hash del
texto en ``data/cap_translations.json``. Runtime lee el caché (instantáneo); si falta una
entrada y Ollama no responde, se devuelve el original (fail-soft, nunca rompe la consola).
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from . import live_data

_OLLAMA = "http://localhost:11434/api/generate"
_MODEL = "qwen3.6:35b-a3b"
_CACHE_REL = "data/cap_translations.json"
# Señales de que un texto YA está en español → no traducir (ahorra llamadas).
_ES_HINT = re.compile(r"[áéíóúñ¿¡]|\b(el|la|los|las|de|para|con|que|usa|cuando|antes)\b", re.I)


def _cache_path(repo: Path | None = None) -> Path:
    return (repo or live_data.DEFAULT_REPO) / _CACHE_REL


def load_cache(repo: Path | None = None) -> dict[str, str]:
    """Carga el caché de traducciones (hash→es). Fail-soft a {}."""
    path = _cache_path(repo)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict[str, str], repo: Path | None = None) -> None:
    path = _cache_path(repo)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
    except OSError:
        pass


def _key(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]


def looks_spanish(text: str) -> bool:
    """Heurística barata: ¿el texto ya parece español? (evita traducir lo propio)."""
    return bool(_ES_HINT.search(text or ""))


def _ollama_translate(text: str) -> str | None:
    """Llama a Ollama para traducir EN→ES. Devuelve None si falla (fail-soft)."""
    prompt = ("Traduce al español neutro el siguiente texto técnico. Responde SOLO con la "
              "traducción, sin comillas, sin notas, sin repetir el original:\n\n" + text)
    body = json.dumps({"model": _MODEL, "prompt": prompt, "stream": False,
                       "think": False, "options": {"temperature": 0.1}}).encode("utf-8")
    req = urllib.request.Request(_OLLAMA, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read()).get("response", "").strip()
        return out or None
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def translate(text: str, cache: dict[str, str], repo: Path | None = None,
              *, allow_call: bool = False) -> str:
    """Traduce ``text`` a español usando el caché; opcionalmente llama a Ollama si falta.

    Args:
        text: texto a traducir.
        cache: caché en memoria (de ``load_cache``); se muta si se traduce algo nuevo.
        allow_call: si True y falta en caché, llama a Ollama y persiste. En runtime de la
            consola se deja False (instantáneo); el pre-poblado lo pone True.

    Returns:
        La traducción en español, o el texto original si ya es español / no hay traducción.
    """
    if not text or looks_spanish(text):
        return text
    k = _key(text)
    if k in cache:
        return cache[k]
    if not allow_call:
        return text
    es = _ollama_translate(text)
    if es:
        cache[k] = es
        _save_cache(cache, repo)
        return es
    return text


def prefill(texts: list[str], repo: Path | None = None) -> dict[str, int]:
    """Pre-traduce una lista de textos al caché (para correr una vez, fuera del request).

    Returns:
        ``{translated, cached, skipped}`` con los conteos.
    """
    cache = load_cache(repo)
    stats = {"translated": 0, "cached": 0, "skipped": 0}
    for t in texts:
        if not t or looks_spanish(t):
            stats["skipped"] += 1
            continue
        if _key(t) in cache:
            stats["cached"] += 1
            continue
        before = len(cache)
        translate(t, cache, repo, allow_call=True)
        stats["translated" if len(cache) > before else "skipped"] += 1
    return stats
