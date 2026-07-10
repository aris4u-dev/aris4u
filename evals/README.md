# evals/ — Benchmark del substrato de memoria (WS-D)

Mide el índice semántico REAL de ARIS4U (`data/aris_vectors.db`, sqlite-vec +
embeddings bge-m3 1024d) con **known-item search**: muestrea items reales del vec
store, deriva una query de cada uno y comprueba si el KNN lo recupera en top-k.

## Por qué known-item (y no un dataset curado)

El antiguo `rag_recall.jsonl` apuntaba a docs de una era purgada (exploitdb/mitre/
code-chunks) que ya **no existen** en el substrato → habría medido recall 0 sobre
datos inexistentes. El known-item evalúa el índice tal como está hoy y no vuelve a
quedar stale cuando cambian los datos. (Ese .jsonl fue eliminado.)

## Correr

```bash
.venv312/bin/python evals/run_rag_recall.py --n 200          # tabla legible
.venv312/bin/python evals/run_rag_recall.py --n 200 --json   # JSON (tracking)
```

Requiere **Ollama vivo** (embeddings) → es una herramienta LOCAL, no de CI. Si el
embedder no responde, sale con código 2 y un mensaje (no rompe nada).

## Métricas

- **modo `exact`**: query = texto completo del item → sanity del índice.
- **modo `partial`**: query = primeras N palabras → recall realista (consulta incompleta).
- **`strict`**: acierto solo si el hit es el mismo `(source, source_id)`.
- **`content`**: acierto si el hit tiene **texto idéntico** → descuenta duplicados de
  narrativa. El gap `strict < content` mide la **redundancia** en `observations`.
- `recall@{1,3,5,10}`, `MRR`, latencia `mean/p50/p95/max` (ms).

## Baseline (`rag_recall_baseline.json`)

Snapshot sobre 9 261 vectores (8 850 observations + 411 decisions), 2026-06-19:

| modo | recall@1 (content) | recall@10 (content) | recall@1 (strict) | p50 |
|------|--------------------|---------------------|-------------------|-----|
| exact   | **0.92** | **0.97** | 0.71 | ~129 ms |
| partial | 0.56 | 0.72 | 0.42 | ~124 ms |

Lecturas: el índice recupera el doc correcto el **92 %** de las veces con query
completa (97 % en top-10) — índice sano. El gap strict↔content (0.71 vs 0.92)
revela **redundancia** en la narrativa (digests de sesión casi idénticos). La
latencia (~125 ms) la domina el `/api/embeddings` de Ollama, no el KNN.

Regenera el baseline tras cambios grandes de substrato o de modelo de embeddings
y compara para detectar regresiones de recall/latencia.

## A/B de embedders (`compare_embedders.py`)

Compara modelos de embedding (misma dim 1024) sobre el substrato real con KNN
coseno en memoria (numpy), sin tocar `data/aris_vectors.db`:

```bash
.venv312/bin/python evals/compare_embedders.py --models bge-m3 mxbai-embed-large --n 150
```

### Resultado 2026-06-19 (137 textos reales) — decisión: **quedarse en bge-m3**

| modelo | recall@5 | MRR | latencia/embed | docs no indexables |
|--------|----------|-----|----------------|--------------------|
| **bge-m3** (actual) | **0.9197** | 0.90 | ~98 ms | 0 |
| mxbai-embed-large | 0.9191 | 0.90 | ~23 ms (4×) | 1 (context 512) |

Recall **idéntico** (Δ=0.0006 = ruido). mxbai es 4× más rápido pero su context de
512 tokens **no indexa docs largos** (digests/observations) → perdería items del
recall; y mezclar query/doc de modelos distintos no es válido (espacios vectoriales
distintos). **Veredicto: bge-m3 confirmado.**

Sobre **Qwen3-Embedding-8B** (propuesto en memoria como "+11 MTEB, mismo 1024d"):
research lo desmiente — es **4096d nativo** (requiere truncado MRL, ~5% pérdida),
**5–6 GB RAM**, **3–4× más lento** (~300–500 ms/embed, y el embed está en el camino
caliente de cada prompt). La ganancia MTEB (+7.58, no +11) es en corpus públicos,
no necesariamente en este KB. **No migrar.** Si algún día se evalúa, probar la
variante **4B** y validar con este A/B ANTES (hubo un revert previo de embedder).
