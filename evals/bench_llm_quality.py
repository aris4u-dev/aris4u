#!/usr/bin/env python3
"""Benchmark de CALIDAD + velocidad de un LLM local vía MLX, con chat template correcto.

Motiva: el smoke inicial de Nemotron-Cascade-2 con `mlx_lm generate --prompt` crudo entró
en loop (no aplicó el chat template del modelo). Este script aplica el template del
tokenizer (apply_chat_template) y corre prompts representativos del trabajo real del usuario
(código en español y en inglés, razonamiento breve) para juzgar la CALIDAD, no solo tok/s.

Todo local (MLX, GPU Neural Accelerators del M5). No red, no datos que salgan.

Uso:
    .venv312/bin/python evals/bench_llm_quality.py \\
        --model mlx-community/Nemotron-Cascade-2-30B-A3B-4bit --max-tokens 700
"""

from __future__ import annotations

import argparse
import sys
import time

# Prompts fijos (deterministas) que espejan el trabajo real: apps Flutter/Dart, backend,
# y una decisión de arquitectura breve. Mezcla ES/EN a propósito (probar el multilingüe).
PROMPTS: list[tuple[str, str]] = [
    (
        "codigo_es_dart",
        "Escribe una función en Dart (Flutter) llamada calcularTotalConImpuesto que recibe "
        "una List<double> precios y un double tasaImpuesto (ej. 0.115), y retorna el total con "
        "impuesto redondeado a 2 decimales. Maneja la lista vacía devolviendo 0.0. Responde solo "
        "el código, sin explicación.",
    ),
    (
        "codigo_en_python",
        "Write a Python function `retry_with_backoff(fn, attempts=3, base_delay=0.5)` that calls "
        "fn(), retries on exception with exponential backoff, and re-raises the last exception if "
        "all attempts fail. Include type hints. Code only.",
    ),
    (
        "razonamiento_es",
        "En 4 frases: ¿cuándo conviene un índice parcial en PostgreSQL en vez de uno normal? "
        "Da un ejemplo concreto.",
    ),
]


def main() -> int:
    """Corre los prompts, mide tok/s por prompt e imprime output completo para juicio."""
    ap = argparse.ArgumentParser(description="Benchmark de calidad+velocidad de un LLM MLX")
    ap.add_argument("--model", required=True, help="Ruta HF del modelo MLX")
    ap.add_argument("--max-tokens", type=int, default=700)
    ap.add_argument("--temp", type=float, default=0.2)
    ap.add_argument("--system", default="", help="System prompt (ej. control de reasoning)")
    args = ap.parse_args()

    try:
        from mlx_lm import generate, load  # type: ignore[import-not-found]
        from mlx_lm.sample_utils import make_sampler  # type: ignore[import-not-found]
    except ImportError as e:
        print(f"[FATAL] mlx_lm no disponible: {e}", file=sys.stderr)
        return 2

    print(f"=== Cargando {args.model} (MLX) ===", flush=True)
    t0 = time.perf_counter()
    model, tokenizer, *_ = load(
        args.model
    )  # load() may return 2 or 3 values (model, tokenizer[, config])
    print(f"Modelo cargado en {time.perf_counter() - t0:.1f}s\n", flush=True)

    sampler = make_sampler(temp=args.temp)
    for name, prompt in PROMPTS:
        messages = ([{"role": "system", "content": args.system}] if args.system else []) + [
            {"role": "user", "content": prompt}
        ]
        formatted = tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False
        )
        print(f"\n{'=' * 70}\n### PROMPT [{name}]\n{'=' * 70}", flush=True)
        t0 = time.perf_counter()
        text = generate(
            model,
            tokenizer,
            prompt=formatted,
            max_tokens=args.max_tokens,
            sampler=sampler,
            verbose=False,
        )
        dt = time.perf_counter() - t0
        n_tok = len(tokenizer.encode(text))  # type: ignore[operator]  # mlx_lm has no stubs; tokenizer type is Unknown
        print(text, flush=True)
        print(
            f"\n--- [{name}] {n_tok} tok en {dt:.1f}s = {n_tok / max(dt, 0.01):.1f} tok/s ---",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
