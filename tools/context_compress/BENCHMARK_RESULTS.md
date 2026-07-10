# ARIS4U L3 Context Compression - Benchmark Results

Date: 2026-07-05  |  Model: microsoft/llmlingua-2-xlm-roberta-large-meetingbank  |  Device: MPS

Payloads: 50 (decision=17, digest=17, observation=16)  |  seed=42
Token counts via tiktoken cl100k (proxy - Claude tokenizer differs ~30%).

## Run 1 - Baseline (no entity forcing, rates 0.33/0.50/0.67)

| Rate (retained) | N | Ratio median | Ratio p10-p90 | Fact-retention median | Fact-ret p10 | Sem-sim median | Lat median ms | Lat p90 ms |
|---|---|---|---|---|---|---|---|---|
| 0.33 | 50 | 3.27x | 2.93x - 3.78x | 0.481 | 0.333 | 0.7985 | 78 | 81 |
| 0.50 | 50 | 2.13x | 1.91x - 2.38x | 0.636 | 0.500 | 0.8704 | 80 | 81 |
| 0.67 | 50 | 1.52x | 1.41x - 1.60x | 0.800 | 0.667 | 0.9295 | 79 | 81 |

## Run 2 - With force_tokens + force_reserve_digit=True (rates 0.50/0.67)

Parameters: `force_tokens=sorted(extract_entities(text))` per payload + `force_reserve_digit=True` via `compress_prompt`.

| Rate (retained) | N | Ratio median | Ratio p10-p90 | Fact-retention median | Fact-ret p10 | Sem-sim median | Lat median ms | Lat p90 ms |
|---|---|---|---|---|---|---|---|---|
| 0.50 | 50 | 1.96x | 1.68x - 2.25x | 0.833 | 0.429 | 0.8671 | 79 | 81 |
| 0.67 | 50 | 1.45x | 1.33x - 1.59x | 0.919 | 0.500 | 0.9262 | 79 | 81 |

## Comparative delta (forced vs baseline, same payloads)

| Rate | Ratio baseline | Ratio forced | Ret baseline | Ret forced | Sim baseline | Sim forced | Lat baseline | Lat forced |
|---|---|---|---|---|---|---|---|---|
| 0.50 | 2.13x | 1.96x | 0.636 | 0.833 | 0.8704 | 0.8671 | 80ms | 79ms |
| 0.67 | 1.52x | 1.45x | 0.800 | 0.919 | 0.9295 | 0.9262 | 79ms | 79ms |

## Verdict

NO-GO even with force. Best forced: rate=0.50, ratio=1.96x, retention=0.833, lat_p90=81ms. Baseline best retention=0.800 at rate=0.67. Conclusion: L3 top ~1.5x on identifier-dense text. Redirect to L2 (relocation, not deletion) for fact-safe compression on this corpus.

## Notes

- Fact-retention = recall of regex-extracted entities (CamelCase, snake_case, numbers >=2 digits, Unix paths) from original in compressed output.
- Semantic similarity = cosine of bge-m3 embeddings (Ollama local).
- GO criteria (forced run): ratio >= 2x AND fact-retention >= 0.95 AND lat_p90 < 500ms.
- force_tokens param: per-payload sorted entity list passed to compress_prompt.
- force_reserve_digit param: bool passed directly to compress_prompt.
- Both runs use seed=42 for identical payload selection.
