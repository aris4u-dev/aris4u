import json
import math
import os
import subprocess
import urllib.request
from typing import Optional

from .config import (
    ARIS4U_ROOT, OLLAMA_MAC_URL, OLLAMA_W2_URL, W2_SSH, MAC_MODELS, W2_MODELS, W2_ENABLED,
    MLX_MODEL, MLX_URL,
)


def dispatch_local(
    prompt: str, model: str = "", system: str = "", timeout: int = 120,
    options: Optional[dict] = None,
) -> Optional[str]:
    model = model or MAC_MODELS["default"]
    payload = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    if options:
        payload["options"] = options
    try:
        result = subprocess.run(
            # Prompt por stdin (-d @-), no por argv: no aparece en `ps` ni topa
            # ARG_MAX. Simetría con dispatch_w2 — privacidad por construcción.
            ["curl", "-s", "--max-time", str(timeout),
             f"{OLLAMA_MAC_URL}/api/generate", "-d", "@-"],
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=timeout + 5,
        )
        data = json.loads(result.stdout)
        # Los modelos de razonamiento (qwen35-analyst) a veces dejan "response"
        # vacío con todo el contenido en "thinking" (done_reason=length, no honran
        # think:False). Surfacear thinking como fallback — si no, se pierde el output
        # real (regresión vs el _call_ollama_role original que ya lo hacía).
        return data.get("response") or data.get("thinking") or None
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
        return None


def dispatch_w2(
    prompt: str, model: str = "", system: str = "", timeout: int = 120,
    options: Optional[dict] = None,
) -> Optional[str]:
    model = model or W2_MODELS["security"]
    body = {
        "model": model,
        "prompt": prompt,
        "system": system,
        "stream": False,
    }
    if options:
        body["options"] = options
    payload = json.dumps(body)
    for attempt in range(2):
        try:
            proc = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10",
                 W2_SSH, "curl", "-s", "--max-time", str(timeout),
                 f"{OLLAMA_W2_URL}/api/generate", "-d", "@-"],
                input=payload, capture_output=True, text=True, timeout=timeout + 15,
            )
            if proc.returncode != 0 and attempt == 0:
                continue
            data = json.loads(proc.stdout)
            return data.get("response") or data.get("thinking") or None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
            if attempt == 0:
                continue
            return None
    return None  # ambos intentos agotados sin respuesta


def _mlx_payload(model: str, system: str, prompt: str, opts: dict) -> dict:
    """Construye el cuerpo OpenAI-compatible para mlx_lm.server.

    Amplificación (F1): thinking OFF. Con thinking ON el MoE gasta el presupuesto de
    tokens razonando y devuelve "content" vacío (caveat medido 2026-06-19); se activa
    solo si el caller lo pide explícito (opts["enable_thinking"] = False).
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": opts.get("num_predict", 400),
        "temperature": opts.get("temperature", 0.3),
        "stream": False,
    }
    if opts.get("enable_thinking") is False:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def _promise_from_logprobs(choice: dict) -> Optional[float]:
    """promise_score = media geométrica de la prob por token = exp(logprob medio) ∈ (0,1].

    Es la confianza intrínseca del cuerpo en su salida (comprimir=predecir: tokens más
    probables → salida más "segura"). Es el `score` que valida la calibración §8.5
    (¿predice el éxito real?). None si la respuesta no trae logprobs.
    """
    content = (choice.get("logprobs") or {}).get("content") or []
    vals = [t["logprob"] for t in content
            if isinstance(t, dict) and isinstance(t.get("logprob"), (int, float))]
    if not vals:
        return None
    return round(math.exp(sum(vals) / len(vals)), 4)


def dispatch_mlx(
    prompt: str, model: str = "", system: str = "", timeout: int = 120,
    options: Optional[dict] = None, score_out: Optional[dict] = None,
) -> Optional[str]:
    """Transporte al mlx_lm.server local (OpenAI-compatible) que sirve el cuerpo MoE.

    LAZY por diseño: NO arranca el server (lo gestiona tools/mlx_serve.sh). Si el server
    no está arriba, curl no responde → devuelve None (fail-open: el router salta a
    Foundation-Sec Mac o W2). Así el MoE (~23GB) solo ocupa RAM mientras el server vive.
    Prompt por stdin (-d @-): no aparece en `ps`, simetría de privacidad con dispatch_local.

    Si ``score_out`` (dict) se pasa, pide logprobs y escribe
    ``score_out['promise_score']`` = confianza del cuerpo (opt-in; el amplificador F1 lo usa).
    """
    model = model or MLX_MODEL
    payload = _mlx_payload(model, system, prompt, options or {})
    if score_out is not None:
        payload["logprobs"] = True
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             f"{MLX_URL}/v1/chat/completions",
             "-H", "Content-Type: application/json", "-d", "@-"],
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=timeout + 5,
        )
        return _parse_mlx_response(result.stdout, score_out)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, ValueError):
        return None


def _parse_mlx_response(stdout: str, score_out: Optional[dict]) -> Optional[str]:
    """Extrae el content de la respuesta del mlx_lm.server; escribe promise_score si procede."""
    if not stdout.strip():
        return None  # server no arrancado / sin respuesta → fail-open
    data = json.loads(stdout)
    choices = data.get("choices") or []
    if not choices:
        return None
    if score_out is not None:
        score_out["promise_score"] = _promise_from_logprobs(choices[0])
    return (choices[0].get("message") or {}).get("content") or None


def _xai_key() -> str:
    """API key de xAI/Grok: env primero, luego el .env del repo (no se exporta al shell)."""
    key = os.environ.get("XAI_API_KEY", "")
    if key:
        return key
    try:
        for line in (ARIS4U_ROOT / ".env").read_text().splitlines():
            if line.startswith("XAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def dispatch_grok(
    prompt: str, model: str = "grok-4-1-fast", system: str = "",
    timeout: int = 60, options: Optional[dict] = None,
) -> Optional[str]:
    """Llama a la API de xAI/Grok (ONLINE — el contenido SALE del host). None si falla.

    SOLO debe invocarse desde model_router.route() tras su gate fail-closed de
    privacidad. NUNCA con PHI / secretos / datos de cliente. La key viaja por
    urllib (en memoria), no por argv: nunca aparece en `ps`.
    """
    key = _xai_key()
    if not key:
        return None
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": messages, "stream": False}
    if options:
        body.update(options)
    try:
        req = urllib.request.Request(
            "https://api.x.ai/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def health_check() -> dict:
    status = {"mac": {"ollama": False, "models": []}, "w2": {"ollama": False, "models": []}}
    try:
        r = subprocess.run(
            ["curl", "-s", "--max-time", "3", f"{OLLAMA_MAC_URL}/api/tags"],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(r.stdout)
        status["mac"]["ollama"] = True
        status["mac"]["models"] = [m["name"] for m in data.get("models", [])]
    except Exception:
        pass

    if W2_ENABLED:
        try:
            r = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=10",
                 W2_SSH, "curl", "-s", "--max-time", "3", f"{OLLAMA_W2_URL}/api/tags"],
                capture_output=True, text=True, timeout=15,
            )
            data = json.loads(r.stdout)
            status["w2"]["ollama"] = True
            status["w2"]["models"] = [m["name"] for m in data.get("models", [])]
        except Exception:
            pass
    else:
        status["w2"]["disabled"] = True  # W2_ENABLED=false: no se intenta (dev sin worker remoto)

    return status
