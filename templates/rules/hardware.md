# HARDWARE — Template (rellenar con `/aris-init` o manualmente)
# Placeholders: reemplazar {{...}} con valores reales de tu máquina.
# Generado por ARIS4U templates/rules/ · instanciar en ~/.claude/rules/hardware.md

Si dudas sobre los valores reales, RE-ESCANEA, no asumas:
- Orquestador (Mac/Linux): `system_profiler SPHardwareDataType SPDisplaysDataType | grep -E "Chip|Cores|Memory"` (macOS) o `lscpu; free -h; nvidia-smi` (Linux)
- Worker remoto: `ssh {{WORKER_HOST}} 'lscpu | grep "Model name"; free -h; nvidia-smi --query-gpu=name,memory.total --format=csv'`

## {{ORCHESTRATOR_HOST}} — orquestador + trabajo local (PRIMARIO)
- **Chip:** {{ORCHESTRATOR_CHIP}} (p.ej. Apple M5 Pro / Intel Core i9 / AMD Ryzen 9)
- **CPU:** {{CPU_CORES}} cores ({{PERF_CORES}} performance + {{EFF_CORES}} efficiency, si aplica)
- **GPU:** {{GPU_CORES}} cores · **backend = {{GPU_BACKEND}}** (MPS / CUDA / CPU-only)
  - MPS (Apple Silicon): usar para inferencia local, compositing, embeddings batch — ~10x vs numpy-CPU
  - CUDA: verificar VRAM antes de cargar modelos
- **RAM:** {{RAM_GB}} GB (unified en Apple Silicon · CPU+GPU comparten)
- **Disco:** {{DISK_SIZE}} ({{DISK_FREE}} libres) · OS: {{OS_VERSION}}
- **Ollama modelos instalados:** {{LOCAL_MODELS_LIST}}
  - Regla GPU: usar el backend acelerado para cómputo (MPS/CUDA); lo prohibido = render WebGL pesado en tiempo real

## {{WORKER_HOST}} — worker remoto (OPCIONAL, verificar antes de despachar)
- **Host:** `ssh {{WORKER_HOST}}` ({{WORKER_TAILSCALE_IP}} si usas Tailscale)
- **CPU:** {{WORKER_CPU_MODEL}} ({{WORKER_CPU_CORES}} cores)
- **RAM:** {{WORKER_RAM_GB}} GB (⚠️ verificar uso antes de despachar: `ssh {{WORKER_HOST}} free -h`)
- **GPU:** {{WORKER_GPU_MODEL}}, {{WORKER_GPU_VRAM}} GB VRAM
- **Disco:** {{WORKER_DISK_SIZE}} ({{WORKER_DISK_FREE}} libres) · OS: {{WORKER_OS}}
- **Ollama modelos:** {{WORKER_MODELS_LIST}}
- **Servicios corriendo siempre:** {{WORKER_SERVICES}} — verificar RAM libre antes de despachar trabajo pesado
- **Regla:** SOLO offload de lo que pertenece al entorno del worker (CUDA/builds Linux); verificar que responde Y que hay RAM libre

## HOSTS MUERTOS — no referenciar, no despachar
{{DEAD_HOSTS}} (p.ej. W1, W3, W4 — sustituir por los que ya no existen en tu setup)

## Cómo exprimir el potencial

### Paralelismo por tipo (ver parallel-dispatch.md para el canon completo)
- **Agentes de razonamiento Claude = NUBE** (~0.3 GB contexto local c/u) → lanza hasta `min(16, CPU_CORES-2)` sin miedo
- **Workflows/agentes con modelos o builds LOCALES** → ≤3-4 simultáneos (consumen GBs reales)
- **Límite real = RAM** (≤{{RAM_SAFE_GB}} GB seguro, swap=0) y GPU (≤{{GPU_SAFE_GB}} GB {{GPU_BACKEND}})

### GPU/backend acelerado
- **{{GPU_BACKEND}} para cómputo local** = recomendado y necesario (embeddings batch, difusión, inferencia)
- Crash documentado: render WebGL pesado en tiempo real = prohibido en el orquestador principal

### Techo de saturación (medir empíricamente en tu hardware)
- RAM: saturación cuando swap > 0 · presupuesto seguro: {{RAM_GB}}GB total − {{RAM_MARGIN_GB}}GB margen = {{RAM_SAFE_GB}}GB
- GPU/Metal: {{GPU_SAFE_GB}} GB (≈75% de RAM unified en Apple Silicon)
- Verificar en vivo: `vm_stat`, `ps -axo pid,comm,rss,etime | sort -k3 -rn | head -20`
