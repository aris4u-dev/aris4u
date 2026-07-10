"""
L3 prompt-compression layer for ARIS4U context pipeline.

Uses LLMLingua-2 (xlm-roberta-large-meetingbank) on MPS when available.
rate = fraction RETAINED (0.33 = keep 33% of tokens).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import torch
from llmlingua import PromptCompressor

_MODEL_ID = "microsoft/llmlingua-2-xlm-roberta-large-meetingbank"
_LOCK = threading.Lock()
_COMPRESSOR: PromptCompressor | None = None

# Derive the expected local cache path for the model.
_HF_HOME = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
_MODEL_CACHE = _HF_HOME / "hub" / "models--microsoft--llmlingua-2-xlm-roberta-large-meetingbank"


def _load_compressor(device: str) -> PromptCompressor:
    """Load PromptCompressor, preferring local cache to avoid hub network checks.

    Args:
        device: Target device string passed to PromptCompressor (e.g. ``"mps"``).

    Returns:
        A ready PromptCompressor instance.
    """
    cached = _MODEL_CACHE.exists()
    prev = os.environ.get("HF_HUB_OFFLINE")
    if cached:
        # Suppress ALL huggingface_hub network calls (including the
        # _patch_mistral_regex model_info() probe inside the tokenizer).
        os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        return PromptCompressor(
            _MODEL_ID,
            use_llmlingua2=True,
            device_map=device,
        )
    finally:
        # Restore previous state so callers are not affected.
        if prev is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = prev


def _get_compressor() -> PromptCompressor:
    global _COMPRESSOR
    if _COMPRESSOR is None:
        with _LOCK:
            if _COMPRESSOR is None:
                device = "mps" if torch.backends.mps.is_available() else "cpu"
                _COMPRESSOR = _load_compressor(device)
    return _COMPRESSOR


def compress(
    text: str,
    rate: float = 0.5,
    force_tokens: list[str] | None = None,
    force_reserve_digit: bool = False,
) -> str:
    """Compress *text* retaining approximately *rate* fraction of tokens.

    Args:
        text: Input text to compress.
        rate: Fraction of tokens to RETAIN (0 < rate <= 1).
              e.g. 0.33 = keep ~33%, 0.5 = keep ~50%, 0.67 = keep ~67%.
        force_tokens: Additional token strings that must not be dropped
            (e.g. per-payload entity list).  ``"\\n"`` is always included.
        force_reserve_digit: When True, all digit tokens are preserved
            unconditionally.  Passed directly to ``compress_prompt``.

    Returns:
        Compressed string.  Returns *text* unchanged when rate >= 1.0.
    """
    if rate >= 1.0:
        return text
    tokens = ["\n"] + (force_tokens or [])
    comp = _get_compressor()
    result = comp.compress_prompt(
        context=[text],
        rate=rate,
        force_tokens=tokens,
        force_reserve_digit=force_reserve_digit,
    )
    return result["compressed_prompt"]
