#!/usr/bin/env python3
"""Servidor local de la Live Console: sirve la pantalla + el CÓDIGO real de cada pieza.

Seguridad (modelo de amenaza):
  - Bind ESTRICTO a 127.0.0.1 (jamás 0.0.0.0); nunca expuesto a la red.
  - Anti-CSRF / DNS-rebinding: todo POST y los streams SSE exigen ser same-origin a localhost
    (``_origin_ok``) — el bind a 127.0.0.1 NO basta contra una página maliciosa en el navegador.
  - Lectura: GET /code, /memory, /telemetry, /hooks son read-only (sqlite mode=ro).
  - Escritura GOBERNADA: /apply y /revert editan el repo (con staleness + tests + revert);
    /mcp invoca las MCP tools REALES del motor (lista blanca) — las read tienen telemetría como
    único efecto; las ``local`` y ``write`` exigen confirm. No es un servidor "solo lectura".
  - /code valida el path contra la raíz del repo (anti path-traversal por resolución real) y solo
    sirve archivos de TEXTO dentro del repo, con cap de tamaño.

Regenera el inventario + la pantalla al arrancar (siempre fresco del código).

Uso:
    python3 -m aris4u_console.server [--port 8787] [--repo PATH] [--no-open]
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import pty
import re
import secrets
import select
import signal
import sqlite3
import struct
import subprocess
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import capabilities, inventory, live_data, render_console

# ---------------------------------------------------------------------------
# Lazy import of tools/project_timeline (lives at the repo root, not inside
# the console package). We add the repo root to sys.path ONCE at import time
# so that ``import tools.project_timeline`` works without hardcoding paths.
# The repo root is three levels above this file:
#   <repo>/console/aris4u_console/server.py → parent.parent.parent = <repo>
# ---------------------------------------------------------------------------
import sys as _sys
_REPO_ROOT_FOR_TOOLS = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT_FOR_TOOLS) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT_FOR_TOOLS))
try:
    from tools import project_timeline as _pt  # type: ignore[import]
    _PT_OK = True
except ImportError:
    _pt = None  # type: ignore[assignment]
    _PT_OK = False

try:
    from tools import cowork_intake as _ci  # type: ignore[import]
    _CI_OK = True
except ImportError:
    _ci = None  # type: ignore[assignment]
    _CI_OK = False

try:
    from tools import cowork_runner as _cr  # type: ignore[import]
    _CR_OK = True
except ImportError:
    _cr = None  # type: ignore[assignment]
    _CR_OK = False

# Cap de body para POST /intake: acepta hasta 10 MB (múltiples docs en base64)
_MAX_INTAKE_BODY_BYTES = 10 * 1024 * 1024

# ---------------------------------------------------------------------------
# Intake helpers (humanización FIX 3)
# ---------------------------------------------------------------------------

_INTAKE_STATUS_LABELS: dict[str, str] = {
    "pending":     "Recibido — pendiente de aprobación por el operador",
    "building":    "En construcción — el build está corriendo",
    "done":        "Completado — el proyecto está listo",
    "failed":      "Falló — hubo un error en el build",
    "rejected":    "Rechazado",
    "in_progress": "En progreso",
}


def _intake_status_label(status: str) -> str:
    """Return a human-readable Spanish label for an intake status.

    Args:
        status: Raw status string from the DB.

    Returns:
        Readable label, or the raw status if not in the known map.
    """
    return _INTAKE_STATUS_LABELS.get(status, status)


# Launcher inyectable para POST /run-intake.  En producción usa el real de cowork_runner;
# los tests reemplazan este módulo-level para evitar invocar claude en ningún test.
# Se asigna después de intentar importar _cr (ver bloque de importación más abajo).
_INTAKE_LAUNCHER = None  # resuelto en _resolve_intake_launcher() al primer uso

HOST = "127.0.0.1"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_MAX_CODE_BYTES = 1_000_000
_MAX_PTY_SESSIONS = 8  # tope de terminales vivas a la vez (evita acumulación de procesos)
_TEXT_SUFFIXES = {".py", ".sh", ".json", ".toml", ".md", ".yaml", ".yml", ".txt",
                  ".sql", ".cfg", ".ini", ".js", ".html", ".css"}
_VENDOR_CTYPES = {".js": "application/javascript", ".css": "text/css"}
# GET que exponen contenido del repo o memoria con secretos cross-client: requieren el
# mismo guard CSRF/DNS-rebinding que los POST (un GET no protegido es leíble por una página
# cross-origin vía DNS-rebinding). curl/Claude no mandan Origin → pasan (no son CSRF).
_SENSITIVE_GETS = {"/code", "/memory", "/memory/facets", "/memory/search",
                   "/project", "/project/stream", "/project/comments",
                   "/intakes"}

# Presets de terminal en LISTA BLANCA (el cliente solo elige una clave; nunca pasa argv libre).
_PRESETS = {
    "shell": ["/bin/zsh", "-l"],
    "claude": ["claude"],
    "local": ["ollama", "run", "qwen3.6:35b-a3b"],
}
_SESSIONS: dict[str, PtySession] = {}
_SESSIONS_LOCK = threading.Lock()

# Manifiesto de la superficie nativa (servido en GET /manifest). Doble propósito: ARIS4U se
# autodescribe y CLAUDE tiene acceso inmediato a TODO ARIS4U sin grepear el código — un
# `curl /manifest` enumera cada brazo (qué consultar, qué devuelve, qué tools invocar). El
# test test_manifest_complete.py exige que cada ruta de do_GET/do_POST esté aquí (anti-drift).
ENDPOINTS: list[dict] = [
    # --- Lectura (GET) — la API nativa que Claude puede curl-ear ---
    {"path": "/manifest", "method": "GET", "kind": "read",
     "purpose": "Este manifiesto: enumera todos los brazos de ARIS4U expuestos por la consola."},
    {"path": "/status", "method": "GET", "kind": "read",
     "purpose": "Salud de cada parte de ARIS4U (verde/naranja/rojo).",
     "returns": "{available, items[], overall, summary}"},
    {"path": "/atoms", "method": "GET", "kind": "read",
     "purpose": "Átomos de método (patrones reutilizables) con todos sus ejes + grafo de transferencia.",
     "returns": "{available, atoms[{decision,problem_class,artifact_type,regime,validity_domain,source_project,transfers_to,...}], totals}"},
    {"path": "/valorizacion", "method": "GET", "kind": "read",
     "purpose": "Score RICE-A+Moat y veredicto adopt/build/omit por átomo.",
     "returns": "{available, atoms[{...,score,verdict,moat}], totals}"},
    {"path": "/auditoria", "method": "GET", "kind": "read",
     "purpose": "Hallazgos de calidad del store: duplicados, sin-validity, sin-source, bajo-valor, huecos.",
     "returns": "{available, findings[], summary, total_findings, total_atoms}"},
    {"path": "/backlog", "method": "GET", "kind": "read",
     "purpose": "Backlog de adopción VERIFICADO (lo accionable de Valorización): qué patrón probado vale construir en cada destino, tras filtrar fit (absent/mismatch/likely-present). Solo candidatos.",
     "returns": "{available, by_project[{project, code, count, items[{pattern, origin, score, moat, verdict, artifact_type, fit, why}]}], total_items, total_projects, fit_totals, filtered_projects, fit_shown}"},
    {"path": "/skeletons", "method": "GET", "kind": "read",
     "purpose": "Catálogo de plantillas de código reutilizables (skeleton por átomo) — exactamente lo que el build flow inyecta al construir en un dominio que matchea un patrón probado.",
     "returns": "{available, by_family[{family, count, items[{name, origin, problem_class, regime, lines, skeleton}]}], total, families}"},
    {"path": "/memory", "method": "GET", "kind": "read",
     "purpose": "Memoria viva: decisiones/guards/digests por cliente + recientes + vectores + recall.",
     "returns": "{available, totals, by_client[], by_source[], recent_decisions[], recent_guards[], recent_digests[]}"},
    {"path": "/memory/facets", "method": "GET", "kind": "read",
     "purpose": "Clientes/proyectos y dominios distintos (para filtrar la búsqueda de memoria).",
     "returns": "{available, clients[], domains[]}"},
    {"path": "/memory/search", "method": "GET", "kind": "read",
     "purpose": "Búsqueda en memoria con filtros.",
     "params": "q, client, domain, locked(0|1), stale(días), limit, mem_type(rule|episode|decision|fact|…)",
     "returns": "{available, decisions[], guards[], count, mem_type_counts{}, mem_type_filter}"},
    {"path": "/telemetry", "method": "GET", "kind": "read",
     "purpose": "Pulso: eventos de hooks en vivo (auto_recall, model_hint, depth_inject…).",
     "params": "limit", "returns": "{available, window, by_type, by_hook, recent[]}"},
    {"path": "/telemetry/stream", "method": "GET", "kind": "stream",
     "purpose": "SSE del pulso en vivo."},
    {"path": "/hooks", "method": "GET", "kind": "read",
     "purpose": "Hooks y guards cableados por evento + qué disparó y cuándo.",
     "returns": "{available, events[], fired_by_source, last_fired}"},
    {"path": "/amplifier", "method": "GET", "kind": "read",
     "purpose": "Estado del cuerpo local (F1) + el lazo usar→medir→cablear.",
     "returns": "{available, body_up, calls, availability_rate, latency_p50, latency_p90, pending[]}"},
    {"path": "/routing", "method": "GET", "kind": "read",
     "purpose": "Observatorio de routing/costo (V18): disciplina de model= en los Agent(), distribución por modelo/intención, costo relativo.",
     "params": "days", "returns": "{available, dispatches, explicit_model, inherited, discipline_pct, session_model, by_model{}, by_intent{}, cost_units_relative}"},
    {"path": "/code", "method": "GET", "kind": "read",
     "purpose": "Lee un archivo del repo (anti path-traversal: path validado contra la raíz).",
     "params": "path=<ruta relativa>"},
    {"path": "/config", "method": "GET", "kind": "read",
     "purpose": "Config efectiva de ARIS4U: modelo por defecto, env/flags, MCP cableados, dónde vive cada cosa.",
     "returns": "{available, model_default, env{}, mcp_global[], mcp_repo[], mcp_duplicated[], mcp_by_source[{name,origin,command}], settings_path}"},
    {"path": "/quality", "method": "GET", "kind": "read",
     "purpose": "Panel de deuda técnica: gate_results de sessions.db agregados por módulo.",
     "returns": "{available, totals{total,clean,issues}, last_gate, top_issues[{module_name,total,clean,issues,last_status,last_ts}]}"},
    {"path": "/briefs", "method": "GET", "kind": "read",
     "purpose": "Últimas sesiones narrativas de claude-mem.db para cebar sesiones nuevas rápido.",
     "params": "limit (default 15, max 50)",
     "returns": "{available, total_in_db, briefs[{id,project,request_short,learned_short,completed_short,created_at}]}"},
    {"path": "/phi-guard-blocks", "method": "GET", "kind": "read",
     "purpose": "Conteo de bloqueos de phi_guard (phi_to_external_blocked) desde el event log.",
     "returns": "{available, total, last_ts, last_tool}"},
    {"path": "/project", "method": "GET", "kind": "read",
     "purpose": "Timeline del proyecto: commits git anotados con el porqué de ARIS4U "
                "(decisions/digests/gates) anclado al HEAD real. Requiere ?client=<id>. "
                "in_progress es efímero (builds activos); desaparece al terminar. "
                "Git es el ancla — nada en in_progress cuenta como progreso.",
     "params": "client (requerido)",
     "returns": "{available, client, count, timeline[{sha, author, date, subject, files, "
                "why{decisions, digests, gates}}], in_progress[{run_id, repo_path, "
                "started_at, status, log_tail}]}"},
    {"path": "/project/stream", "method": "GET", "kind": "stream",
     "purpose": "SSE del timeline: emite un evento cuando el HEAD SHA o los comentarios cambian. "
                "Requiere ?client=<id>."},
    {"path": "/project/comments", "method": "GET", "kind": "read",
     "purpose": "Lista de comentarios de cowork anclados a un commit SHA. "
                "Requiere ?client=<id>&sha=<sha>.",
     "params": "client (requerido), sha (requerido)",
     "returns": "[{id, sha, author, role, body, created_at}]"},
    # --- Operación (POST) — los brazos que Claude/ARIS4U accionan ---
    {"path": "/mcp", "method": "POST", "kind": "operate",
     "purpose": "Invoca un tool MCP REAL del motor (lista blanca).",
     "body": "{tool, args}",
     "tools": ["aris_search", "aris_recall_client", "aris_ingest",
               "aris_dialectic", "aris_structure", "aris_critique", "aris_health"]},
    {"path": "/amplifier/label", "method": "POST", "kind": "operate",
     "purpose": "Etiqueta una propuesta del amplificador (cierra el lazo de calibración F1).",
     "body": "{id, label}"},
    {"path": "/project/comment", "method": "POST", "kind": "write",
     "purpose": "Inserta un comentario de cowork anclado a un commit SHA. "
                "Única escritura del subsistema /project; va solo a cowork_comments.",
     "body": "{sha, author, role, body, client}",
     "returns": "{ok, id}"},
    {"path": "/intake", "method": "POST", "kind": "write",
     "purpose": "Crea un intake de proyecto (brief + docs opcionales). Superficie no-técnica: "
                "el CEO/fundador describe lo que quiere y sube docs; dispara el pipeline de build.",
     "body": "{client, brief, docs:[{name, content_b64}]}",
     "returns": "{ok, intake_id, status, status_label, next_step, skipped_docs}"},
    {"path": "/intakes", "method": "GET", "kind": "read",
     "purpose": "Lista los intakes existentes (opcionalmente filtrados por ?status=<s>).",
     "params": "status (pending|in_progress|done|rejected, opcional)",
     "returns": "{available, intakes[{id, client_id, status, status_label, brief_preview, created_at}]}"},
    {"path": "/run-intake", "method": "POST", "kind": "operate",
     "purpose": "Acción de OPERADOR: aprueba un intake pending y lanza su build en background. "
                "Acepta {intake_id} o {client} (usa el pending más antiguo del cliente). "
                "Responde inmediatamente; el progreso aparece en GET /project?client=<id>.",
     "body": "{intake_id} | {client}",
     "returns": "{ok, intake_id, status, message}"},
    {"path": "/review", "method": "POST", "kind": "operate",
     "purpose": "Revisa un cambio de código propuesto (staleness + tests) ANTES de aplicar.",
     "body": "{path, content}"},
    {"path": "/apply", "method": "POST", "kind": "write",
     "purpose": "Aplica un cambio de código gobernado (staleness + tests + revert).",
     "body": "{path, content}"},
    {"path": "/revert", "method": "POST", "kind": "write",
     "purpose": "Revierte el último cambio aplicado.", "body": "{path}"},
    {"path": "/regenerate", "method": "POST", "kind": "operate",
     "purpose": "Regenera el inventario + el HTML de la consola desde la verdad viva."},
    {"path": "/cap/skills", "method": "GET", "kind": "read",
     "purpose": "Skills (Claude + ARIS4U) con audit por valor (estado/uso/redundancia/veredicto)."},
    {"path": "/cap/agents", "method": "GET", "kind": "read",
     "purpose": "Agent types (Claude + ARIS4U) con uso real de la telemetría + veredicto."},
    {"path": "/cap/mcp", "method": "GET", "kind": "read",
     "purpose": "MCP servers (Claude + ARIS4U) con uso real (mcp_call) + veredicto."},
    {"path": "/cap/api", "method": "GET", "kind": "read",
     "purpose": "API/capacidades: modelos de Claude + endpoints/hooks de ARIS4U."},
    {"path": "/cap/test/skills", "method": "GET", "kind": "read",
     "purpose": "Smoke test de skills: archivo + frontmatter válido (name+description)."},
    {"path": "/cap/test/agents", "method": "GET", "kind": "read",
     "purpose": "Smoke test de agents: archivo + frontmatter válido."},
    {"path": "/cap/test/mcp", "method": "GET", "kind": "read",
     "purpose": "Smoke test de MCP: binario del comando presente + tools del server propio."},
    {"path": "/cap/test/api", "method": "GET", "kind": "read",
     "purpose": "Smoke test de API: modelos configurados + endpoints + hooks cableados."},
]


class PtySession:
    """Una sesión de terminal: un comando corriendo en un PTY (read-only del lado servidor).

    Corre con los permisos del usuario, sin elevar. El servidor solo bombea bytes entre el
    PTY y el navegador (SSE para salida, POST para entrada).
    """

    def __init__(self, argv: list[str], cwd: str) -> None:
        self.argv = argv
        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # hijo: se convierte en el comando
            try:
                os.chdir(cwd)
            except OSError:
                pass
            os.environ["TERM"] = "xterm-256color"
            try:
                os.execvp(argv[0], argv)
            except OSError:
                os.write(2, f"no se pudo ejecutar {argv[0]}\n".encode())
                os._exit(127)

    def read(self) -> bytes | None:
        """Lee del PTY: bytes con datos, b'' si EOF, None si timeout (sin datos aún)."""
        try:
            r, _, _ = select.select([self.fd], [], [], 0.5)
        except OSError:
            return b""
        if self.fd in r:
            try:
                return os.read(self.fd, 65536)
            except OSError:
                return b""
        return None

    def write(self, data: bytes) -> None:
        """Escribe entrada (teclas) al PTY."""
        try:
            os.write(self.fd, data)
        except OSError:
            pass

    def resize(self, rows: int, cols: int) -> None:
        """Ajusta el tamaño de la ventana del PTY."""
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def alive(self) -> bool:
        """True si el proceso hijo sigue vivo."""
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
            return pid == 0
        except OSError:
            return False

    def kill(self) -> None:
        """Mata el proceso y cierra el PTY."""
        for fn in (lambda: os.kill(self.pid, signal.SIGKILL), lambda: os.close(self.fd)):
            try:
                fn()
            except OSError:
                pass


def _hostname(value: str) -> str:
    """Extrae el hostname (sin esquema ni puerto) de un header Host/Origin/Referer."""
    v = value.strip()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("["):  # IPv6 literal: [::1]:port
        return v[1:v.index("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if ":" in v else v


def safe_repo_path(repo: Path, rel: str) -> Path | None:
    """Resuelve ``rel`` DENTRO de ``repo`` o devuelve None (anti path-traversal).

    Args:
        repo: Raíz permitida (se resuelve a ruta real).
        rel: Path relativo pedido por el cliente (no confiable).

    Returns:
        El Path real si es un archivo de texto dentro del repo y bajo el cap; si no, None.
    """
    base = repo.resolve()
    try:
        target = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    if base != target and base not in target.parents:
        return None  # traversal / fuera del repo
    if not target.is_file() or target.suffix.lower() not in _TEXT_SUFFIXES:
        return None
    try:
        if target.stat().st_size > _MAX_CODE_BYTES:
            return None
    except OSError:
        return None
    return target


def _venv_python(repo: Path) -> str:
    """Python del venv del repo (.venv312) para correr ruff/pytest/route_local; fallback python3."""
    p = repo / ".venv312" / "bin" / "python"
    return str(p) if p.exists() else "python3"


def file_hash(path: Path) -> str:
    """Hash corto del contenido en disco (para el staleness check)."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def lint_content(repo: Path, name: str, content: str) -> str:
    """Corre ruff sobre el contenido propuesto (vía stdin); devuelve issues o 'limpio'."""
    try:
        r = subprocess.run(
            [_venv_python(repo), "-m", "ruff", "check", "--stdin-filename", name, "-"],
            input=content, capture_output=True, text=True, timeout=30, cwd=str(repo))
        out = (r.stdout + r.stderr).strip()
        return out or "ruff: sin problemas ✓"
    except (subprocess.SubprocessError, OSError) as e:
        return f"(ruff no disponible: {e})"


def local_critique(repo: Path, rel: str, content: str) -> str:
    """Pide al modelo LOCAL (route_local de ARIS4U) una crítica breve del cambio (best-effort)."""
    code = ("import sys; from engine.v16.model_router import route_local; "
            "r = route_local('critique', sys.stdin.read(), timeout=25); "
            "t = getattr(r, 'text', None); sys.stdout.write(t or '')")
    prompt = (f"Critica breve y concisa de este cambio de código ({rel}). "
              f"Señala bugs, riesgos o mejoras. Si está bien, dilo:\n\n{content[:4000]}")
    try:
        r = subprocess.run(
            [_venv_python(repo), "-c", code], input=prompt, capture_output=True, text=True,
            timeout=45, cwd=str(repo), env={**os.environ, "PYTHONPATH": str(repo)})
        return r.stdout.strip() or "(el modelo local no dio respuesta — crítica omitida; los guards y tests sí corrieron)"
    except (subprocess.SubprocessError, OSError):
        return "(modelo local no disponible — crítica omitida)"


def run_tests(repo: Path, stem: str) -> dict:
    """Corre los tests que referencian al componente (pytest -k stem); devuelve ok + resumen."""
    try:
        r = subprocess.run(
            [_venv_python(repo), "-m", "pytest", "-q", "-k", stem, "--no-header"],
            capture_output=True, text=True, timeout=150, cwd=str(repo))
        lines = (r.stdout + r.stderr).strip().splitlines()
        return {"ok": r.returncode == 0, "summary": "\n".join(lines[-4:]) or "(sin salida)"}
    except (subprocess.SubprocessError, OSError) as e:
        return {"ok": False, "summary": f"(no se pudo correr pytest: {e})"}


def git_revert(repo: Path, rel: str) -> bool:
    """Revierte un archivo a la versión de git (descarta el cambio aplicado)."""
    try:
        r = subprocess.run(["git", "checkout", "--", rel], cwd=str(repo),
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


# --- A3: invocación de las MCP tools (en el venv del motor) ----------------------------
# LISTA BLANCA: el cliente solo elige una clave; el servidor decide args válidos y timeout.
# kind=read (rápido, sin efectos) · local (usa Ollama/MLX, lento) · write (modifica la memoria).
_MCP_TOOLS = {
    "aris_health":        {"args": [], "kind": "read", "timeout": 35},
    "aris_search":        {"args": ["query", "client"], "kind": "read", "timeout": 35},
    "aris_recall_client": {"args": ["client_name", "query", "limit"], "kind": "read", "timeout": 35},
    "aris_structure":     {"args": ["idea"], "kind": "local", "timeout": 120},
    "aris_critique":      {"args": ["response", "angles"], "kind": "local", "timeout": 120},
    "aris_dialectic":     {"args": ["task", "file_path"], "kind": "local", "timeout": 150},
    "aris_ingest":        {"args": ["content", "content_type", "domain", "rationale",
                                    "client", "locked"], "kind": "write", "timeout": 35},
}

# Snippet que corre en el venv del motor: lee {tool,args} de stdin, invoca la función real
# y escribe su texto. Defensa en profundidad: re-valida el tool contra una lista blanca EMBEBIDA
# antes del getattr — así un fallo del guard del servidor no se convierte en getattr arbitrario.
_MCP_RUNNER = (
    "import sys, json; p = json.load(sys.stdin); "
    "allow = {'aris_health','aris_search','aris_recall_client','aris_structure',"
    "'aris_critique','aris_dialectic','aris_ingest'}; "
    "assert p['tool'] in allow, 'tool no permitida'; "
    "import integrations.mcp_server as m; "
    "fn = getattr(m, p['tool']); "
    "sys.stdout.write(fn(**p['args']) or '')"
)


def invoke_mcp(repo: Path, tool: str, args: dict) -> dict:
    """Invoca una MCP tool de la lista blanca por subprocess en el venv del motor.

    Args:
        repo: Raíz del repo ARIS4U (cwd + PYTHONPATH para importar el servidor MCP).
        tool: Nombre de la tool (debe estar en ``_MCP_TOOLS``).
        args: Argumentos crudos del cliente (se filtran a los permitidos por la tool).

    Returns:
        Dict ``{ok, tool, kind, output}`` (o ``{ok: False, error}`` si falla).
    """
    spec = _MCP_TOOLS.get(tool)
    if spec is None:
        return {"ok": False, "error": "tool no permitida"}
    safe_args = {k: args[k] for k in spec["args"] if k in args}
    payload = json.dumps({"tool": tool, "args": safe_args})
    # Best-effort: redirigir la telemetría del motor a un log local de la consola para no
    # contaminar el log de producción / la métrica del freeze. (mcp_server._telemetry hoy
    # hardcodea su ruta y NO honra esta env — pendiente fix de consistencia de 2 líneas en el
    # motor cuando se levante el freeze; aquí queda forward-compatible y sin efecto adverso.)
    console_log = Path(__file__).resolve().parent.parent / "out" / "mcp-telemetry.jsonl"
    try:
        r = subprocess.run(
            [_venv_python(repo), "-c", _MCP_RUNNER], input=payload,
            capture_output=True, text=True, timeout=spec["timeout"], cwd=str(repo),
            env={**os.environ, "PYTHONPATH": str(repo), "ARIS4U_LOG_FILE": str(console_log)})
        out = r.stdout.strip() or (r.stderr.strip()[-800:] if r.returncode else "(sin salida)")
        return {"ok": r.returncode == 0, "tool": tool, "kind": spec["kind"], "output": out}
    except subprocess.TimeoutExpired:
        return {"ok": False, "tool": tool, "kind": spec["kind"],
                "error": f"timeout ({spec['timeout']}s) — el modelo local puede estar frío"}
    except (subprocess.SubprocessError, OSError) as e:
        return {"ok": False, "tool": tool, "kind": spec["kind"], "error": str(e)[:200]}


def _make_handler(repo: Path, out_dir: Path) -> type[BaseHTTPRequestHandler]:
    """Construye el handler HTTP ligado a un repo + carpeta de salida."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # silenciar log ruidoso
            del format, args

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, ctype: str) -> None:
            if not path.is_file():
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            self._send(200, path.read_bytes(), ctype)

        def _serve_code(self, query: str) -> None:
            rel = (parse_qs(query).get("path") or [""])[0]
            target = safe_repo_path(repo, rel)
            if target is None:
                self._send(403, json.dumps({"error": "ruta no permitida"}).encode(),
                           "application/json; charset=utf-8")
                return
            payload = {"path": rel, "hash": file_hash(target),
                       "content": target.read_text(encoding="utf-8", errors="replace")}
            self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _json(self, obj: dict) -> None:
            self._send(200, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _review(self, body: dict) -> None:
            target = safe_repo_path(repo, body.get("path", ""))
            if target is None:
                self._send(403, b'{"error":"ruta no permitida"}', "application/json")
                return
            content = body.get("content", "")
            self._json({
                "stale": bool(body.get("base_hash")) and file_hash(target) != body["base_hash"],
                "lint": lint_content(repo, target.name, content),
                "critique": local_critique(repo, body.get("path", ""), content),
            })

        def _apply(self, body: dict) -> None:
            rel = body.get("path", "")
            target = safe_repo_path(repo, rel)
            if target is None:
                self._send(403, b'{"error":"ruta no permitida"}', "application/json")
                return
            if body.get("base_hash") and file_hash(target) != body["base_hash"]:
                self._json({"applied": False, "stale": True})
                return
            try:
                target.write_text(body.get("content", ""), encoding="utf-8")
            except OSError as exc:
                # Disco lleno, path no escribible, etc. → respuesta limpia sin traceback crudo.
                payload = json.dumps({"applied": False, "error": str(exc)[:200]},
                                     ensure_ascii=False).encode()
                self._send(500, payload, "application/json")
                return
            self._json({"applied": True, "stale": False, "hash": file_hash(target),
                        "test": run_tests(repo, target.stem)})

        def _revert(self, body: dict) -> None:
            rel = body.get("path", "")
            target = safe_repo_path(repo, rel)
            if target is None:
                self._send(403, b'{"error":"ruta no permitida"}', "application/json")
                return
            ok = git_revert(repo, rel)
            self._json({"reverted": ok, "hash": file_hash(target),
                        "content": target.read_text(encoding="utf-8", errors="replace")})

        def _origin_ok(self) -> bool:
            """Defensa CSRF / DNS-rebinding: la petición debe ser same-origin a localhost.

            - Host debe resolver a localhost (bloquea DNS-rebinding: un dominio del atacante que
              rebinde a 127.0.0.1 envía Host = su-dominio, no 127.0.0.1).
            - Si hay Origin/Referer (toda petición de navegador con efectos), su host debe ser local.
            - Sec-Fetch-Site=cross-site se rechaza.
            Clientes no-navegador (curl) no mandan Origin/Sec-Fetch-Site → permitidos (no son CSRF).
            """
            host = _hostname(self.headers.get("Host", ""))
            if host and host not in _LOCAL_HOSTS:
                return False
            if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
                return False
            origin = self.headers.get("Origin") or self.headers.get("Referer")
            return not (origin and _hostname(origin) not in _LOCAL_HOSTS)

        # --- Track A: conductas vivas (lectura read-only; /mcp opera tools reales) ---

        def _manifest(self) -> None:
            """Manifiesto autodescriptivo: acceso nativo de Claude/ARIS4U a TODA la superficie."""
            self._json({
                "name": "ARIS4U Live Console",
                "purpose": ("Superficie de acceso nativo a TODO ARIS4U: ARIS4U se autoexamina y "
                            "Claude consulta/opera cada brazo de un tiro (curl). Los GET son la API "
                            "de lectura; los POST /mcp y /amplifier/label operan los tools reales."),
                "base_url": f"http://{HOST}:<port>",
                "count": len(ENDPOINTS),
                "endpoints": ENDPOINTS,
            })

        def _status(self) -> None:
            """Tablero de estado: cada parte de ARIS4U en verde/naranja/rojo (fail-soft)."""
            try:
                self._json(live_data.read_status(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _atoms(self) -> None:
            """Átomos de método: patrones reutilizables con su composición/uso (fail-soft)."""
            try:
                self._json(live_data.read_atoms(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _capability(self, category: str) -> None:
            """Audit de una categoría de capacidad (skills/agents/mcp/api), fail-soft."""
            self._json(capabilities.read_capability(category, repo))

        def _capability_test(self, category: str) -> None:
            """Smoke test de una categoría: ¿cada capacidad está bien formada y alcanzable?"""
            self._json(capabilities.health(category, repo))

        def _valorizacion(self) -> None:
            """Valorización RICE-A+Moat: score y veredicto adopt/build/omit por átomo."""
            try:
                self._json(live_data.read_valorizacion(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _auditoria(self) -> None:
            """Auditoría del store: duplicados, huecos, bajo-valor, sin-validity."""
            try:
                self._json(live_data.read_auditoria(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _backlog(self) -> None:
            """Backlog de adopción: qué patrón probado adoptar en cada proyecto (fail-soft)."""
            try:
                self._json(live_data.read_backlog(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _skeletons(self) -> None:
            """Catálogo de plantillas reutilizables (lo que el build flow inyecta). Fail-soft."""
            try:
                self._json(live_data.read_skeletons(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _memory(self) -> None:
            """Panel de memoria: sessions.db (por cliente + recientes) + vectores + recall."""
            try:
                self._json(live_data.read_memory(repo))
            except Exception:  # defensa: un bug del lector NO debe 500ear (se vería como 'offline')
                self._json({"available": False, "reason": "error interno del lector"})

        def _memory_facets(self) -> None:
            """Clientes/proyectos y dominios distintos (para los selects del navegador)."""
            try:
                self._json(live_data.memory_facets(repo))
            except Exception:
                self._json({"available": False, "clients": [], "domains": []})

        def _memory_search(self, query: str) -> None:
            """Navegador de memoria: búsqueda + filtros (texto/cliente/dominio/locked/stale)."""
            qs = parse_qs(query)

            def _g(k: str, d: str = "") -> str:
                return (qs.get(k) or [d])[0]

            try:
                limit = max(1, min(200, int(_g("limit", "80"))))
            except ValueError:
                limit = 80
            try:
                stale = max(0, int(_g("stale", "0")))
            except ValueError:
                stale = 0
            try:
                self._json(live_data.search_memory(
                    repo, q=_g("q"), client=_g("client"), domain=_g("domain"),
                    locked=_g("locked") == "1", stale_days=stale, limit=limit,
                    mem_type=_g("mem_type")))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _telemetry(self, query: str) -> None:
            """Telemetría: eventos recientes + agregados por tipo (de v16.1-events.jsonl)."""
            try:
                limit = max(1, min(200, int((parse_qs(query).get("limit") or ["60"])[0])))
            except ValueError:
                limit = 60
            try:
                self._json(live_data.read_telemetry(repo, limit=limit))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _hooks(self) -> None:
            """Estado de hooks/guards: cableado (repo + global) + qué disparó."""
            try:
                self._json(live_data.read_hooks(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _config(self) -> None:
            """Config efectiva de ARIS4U: modelo por defecto, env/flags, MCP cableados (fail-soft)."""
            try:
                self._json(live_data.read_config(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _amplifier(self) -> None:
            """Cabina del amplificador F1: cuerpo, ROI, progreso N/30 y pendientes."""
            try:
                self._json(live_data.read_amplifier(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _routing(self, query: str) -> None:
            """Observatorio de routing/costo (V18): disciplina de model= en los Agent()."""
            try:
                days = max(1, min(90, int((parse_qs(query).get("days") or ["7"])[0])))
            except Exception:
                days = 7
            try:
                self._json(live_data.read_routing(repo, days=days))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _quality(self) -> None:
            """Panel de deuda técnica: gate_results por módulo (los 15 peores)."""
            try:
                self._json(live_data.read_quality(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _briefs(self, query: str) -> None:
            """Últimas sesiones narrativas de claude-mem.db para cebar sesiones nuevas."""
            try:
                limit = max(1, min(50, int((parse_qs(query).get("limit") or ["15"])[0])))
            except ValueError:
                limit = 15
            try:
                self._json(live_data.read_session_briefs(limit=limit))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _phi_guard_blocks(self) -> None:
            """Bloqueos de phi_guard: conteo total desde logs/v16.1-events.jsonl."""
            try:
                self._json(live_data.read_phi_guard_blocks(repo))
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        # --- Cowork / project timeline -------------------------------------------

        def _sessions_db_path(self) -> Path:
            """Resolve sessions.db the same way live_data and project_timeline do."""
            return repo / "data" / "sessions.db"

        def _project_timeline(self, query: str) -> None:
            """GET /project?client=<id> — timeline de commits anotado con ARIS4U intent.

            Requires ``client`` query param; returns 400 without it to prevent
            silent cross-client leakage.  Sensitive: guarded by _SENSITIVE_GETS
            (same CSRF/DNS-rebinding guard as /code and /memory).

            FIX 1: resolves the client's own repo via repo_for_client() instead
            of hardcoding the ARIS4U repo root.  If the client has no build_run
            yet, returns a clean empty timeline (never falls back to ARIS4U repo).
            """
            if not _PT_OK or _pt is None:
                self._json({"available": False, "reason": "project_timeline no disponible"})
                return
            client = (parse_qs(query).get("client") or [""])[0].strip()
            if not client:
                self._send(
                    400,
                    b'{"error": "client es requerido (?client=<id>)"}',
                    "application/json; charset=utf-8",
                )
                return
            try:
                db = self._sessions_db_path()
                in_progress = _pt.active_builds(db_path=db, client_id=client)

                # Resolve the client's repo from build_runs (FIX 1).
                # Falls back gracefully: no build_run → empty timeline, never ARIS4U repo.
                client_repo: str | None = None
                if _CR_OK and _cr is not None:
                    client_repo = _cr.repo_for_client(db_path=db, client_id=client)

                if client_repo is None:
                    # No build run for this client yet — return clean empty state.
                    self._json({
                        "available": True,
                        "client": client,
                        "count": 0,
                        "timeline": [],
                        "in_progress": in_progress,
                        "note": "Aún no hay build para este cliente",
                    })
                    return

                timeline = _pt.build_timeline(
                    repo_path=client_repo,
                    client_id=client,
                    db_path=db,
                )
                self._json({"available": True, "client": client,
                            "count": len(timeline), "timeline": timeline,
                            "in_progress": in_progress})
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _project_stream(self, query: str) -> None:
            """GET /project/stream?client=<id> — SSE que detecta cambios en HEAD o comentarios.

            Emits a ``data:`` event when the HEAD SHA or the max(id) of
            cowork_comments changes relative to the previous tick.  Sends
            ``: ping`` every second while idle, identical to /telemetry/stream.
            Sensitive: guarded by _SENSITIVE_GETS (same check as /telemetry/stream).
            """
            client = (parse_qs(query).get("client") or [""])[0].strip()
            if not client:
                self._send(
                    400,
                    b"client es requerido (?client=<id>)",
                    "text/plain; charset=utf-8",
                )
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self._tail_project(client)
            except (BrokenPipeError, ConnectionError, OSError):
                pass

        def _tail_project(self, client: str) -> None:
            """Inner loop for /project/stream: detect HEAD/comment/build changes and emit events."""
            last_head: str = ""
            last_comment_id: int = -1
            last_build_sig: str = ""

            def _current_head() -> str:
                """Return HEAD SHA of the CLIENT's repo (not the ARIS4U repo).

                Resolves the client's repo path via repo_for_client() from
                cowork_runner — the same mechanism _project_timeline uses (FIX 1).
                If the client has no build_run yet, returns "" cleanly without
                falling back to the ARIS4U repo, so the SSE stream stays idle
                rather than emitting spurious ARIS4U commits.
                """
                if not _CR_OK or _cr is None:
                    return ""
                db = self._sessions_db_path()
                client_repo = _cr.repo_for_client(db_path=db, client_id=client)
                if client_repo is None:
                    return ""
                try:
                    r = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=client_repo,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    return r.stdout.strip() if r.returncode == 0 else ""
                except (subprocess.SubprocessError, OSError):
                    return ""

            def _max_comment_id() -> int:
                if not _PT_OK or _pt is None:
                    return -1
                db = self._sessions_db_path()
                conn = None
                try:
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
                    # Scope al client del stream: no despertar con comentarios de
                    # otro cliente (cowork_comments.client_id, parametrizado).
                    row = conn.execute(
                        "SELECT MAX(id) FROM cowork_comments WHERE client_id = ?",
                        (client,),
                    ).fetchone()
                    return int(row[0]) if row and row[0] is not None else -1
                except (sqlite3.Error, OSError):
                    return -1
                finally:
                    if conn:
                        conn.close()

            def _build_sig() -> str:
                """Return a signature string that changes when active builds change.

                Combines max(started_at) + max(ended_at) of build_runs for this
                client plus the mtime of the active run's build.log (if any).
                Scoped strictly to client_id — no cross-client leakage.
                """
                if not _PT_OK or _pt is None:
                    return ""
                db = self._sessions_db_path()
                conn = None
                try:
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
                    row = conn.execute(
                        "SELECT MAX(started_at), MAX(ended_at), MAX(status) "
                        "FROM build_runs WHERE client_id = ?",
                        (client,),
                    ).fetchone()
                    sig = f"{row[0]}|{row[1]}|{row[2]}" if row else ""
                    # Also detect log growth: append mtime of active run's log.
                    log_row = conn.execute(
                        "SELECT log_path FROM build_runs "
                        "WHERE client_id = ? AND status = 'running' "
                        "ORDER BY started_at DESC LIMIT 1",
                        (client,),
                    ).fetchone()
                    if log_row and log_row[0]:
                        try:
                            mtime = Path(log_row[0]).stat().st_mtime
                            sig += f"|{mtime:.0f}"
                        except OSError:
                            pass
                    return sig
                except (sqlite3.Error, OSError):
                    return ""
                finally:
                    if conn:
                        conn.close()

            while True:
                head = _current_head()
                cid = _max_comment_id()
                bsig = _build_sig()
                if head != last_head or cid != last_comment_id or bsig != last_build_sig:
                    last_head = head
                    last_comment_id = cid
                    last_build_sig = bsig
                    payload = json.dumps(
                        {"head": head, "last_comment_id": cid, "client": client},
                        ensure_ascii=False,
                    )
                    self._sse_write(b"data: " + payload.encode("utf-8") + b"\n\n")
                else:
                    self._sse_write(b": ping\n\n")
                time.sleep(1.0)

        def _project_comment(self, body: dict) -> None:
            """POST /project/comment — insert a cowork comment anchored to a commit SHA.

            Same CSRF/DNS-rebinding guard as all other POST handlers (applied by
            do_POST before routing).  Validates sha and body non-empty.
            """
            if not _PT_OK or _pt is None:
                self._send(
                    503,
                    b'{"ok": false, "error": "project_timeline no disponible"}',
                    "application/json; charset=utf-8",
                )
                return
            # Caps de longitud por campo (el body total ya está limitado a 1 MB
            # por _read_body; esto acota cada campo antes de persistir).
            sha = (body.get("sha") or "").strip()[:64]
            comment_body = (body.get("body") or "").strip()[:8000]
            author = (body.get("author") or "").strip()[:120]
            role = (body.get("role") or "dev").strip()[:40]
            client = (body.get("client") or "").strip()[:120]
            if not sha or not comment_body:
                self._send(
                    400,
                    b'{"ok": false, "error": "sha y body son requeridos"}',
                    "application/json; charset=utf-8",
                )
                return
            try:
                row_id = _pt.add_comment(
                    db_path=self._sessions_db_path(),
                    commit_sha=sha,
                    author=author or "anonymous",
                    role=role,
                    body=comment_body,
                    client_id=client,
                )
                self._json({"ok": True, "id": row_id})
            except Exception as exc:
                payload = json.dumps({"ok": False, "error": str(exc)[:200]},
                                     ensure_ascii=False).encode()
                self._send(500, payload, "application/json; charset=utf-8")

        def _project_comments(self, query: str) -> None:
            """GET /project/comments?client=<id>&sha=<sha> — lista comentarios de un commit.

            Read-only companion to POST /project/comment.  Returns all comments
            for the given (client, sha) pair ordered by creation time.
            Sensitive: guarded by _SENSITIVE_GETS (cross-origin check applied
            in do_GET before routing, same as /project and /memory).
            """
            if not _PT_OK or _pt is None:
                self._json({"available": False, "reason": "project_timeline no disponible"})
                return
            params = parse_qs(query)
            client = (params.get("client") or [""])[0].strip()
            sha = (params.get("sha") or [""])[0].strip()
            if not client or not sha:
                self._send(
                    400,
                    b'{"error": "client y sha son requeridos"}',
                    "application/json; charset=utf-8",
                )
                return
            try:
                all_comments = _pt.list_comments(
                    db_path=self._sessions_db_path(),
                    commit_sha=sha,
                )
                # list_comments does not filter by client; apply client filter here
                # so that different clients cannot read each other's comments.
                comments = [c for c in all_comments if c.get("client_id") == client]
                self._json({"available": True, "client": client, "sha": sha,
                            "comments": comments})
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        # --- End cowork / project timeline ---------------------------------------

        # --- Intake — superficie no-técnica de captura de proyectos -------------

        def _post_intake(self, body: dict) -> None:
            """POST /intake — crea un intake de proyecto.

            Acepta JSON ``{client, brief, docs:[{name, content_b64}]}``.
            Los docs son opcionales; ``content_b64`` es el contenido del archivo
            en base64 (para transporte JSON puro, sin multipart).
            El guard CSRF/same-origin lo aplica ``do_POST`` antes de rutear.

            Args:
                body: Dict ya parseado del body JSON de la petición.
            """
            if not _CI_OK or _ci is None:
                self._send(
                    503,
                    b'{"ok": false, "error": "cowork_intake no disponible"}',
                    "application/json; charset=utf-8",
                )
                return

            # M2: normaliza a lowercase y valida allowlist antes de cualquier otra cosa.
            # create_intake también valida, pero hacerlo aquí da un 400 limpio sin
            # tocar el FS ni la DB.
            client_raw = (body.get("client") or "").strip().lower()[:120]
            brief = (body.get("brief") or "").strip()[:20_000]
            if not client_raw or not brief:
                self._send(
                    400,
                    b'{"ok": false, "error": "client y brief son requeridos"}',
                    "application/json; charset=utf-8",
                )
                return
            if not re.match(r"^[a-z0-9_-]+$", client_raw):
                payload = json.dumps(
                    {"ok": False, "error": (
                        f"client '{client_raw}' inválido: solo letras minúsculas, "
                        "dígitos, guion y guion_bajo (^[a-z0-9_-]+$)"
                    )},
                    ensure_ascii=False,
                ).encode()
                self._send(400, payload, "application/json; charset=utf-8")
                return

            # Decodificar docs: [{name, content_b64}] → [{name, content: bytes}]
            # Los docs con base64 corrupto se omiten; se informan en skipped_docs.
            raw_docs: list[object] = body.get("docs") or []
            doc_files: list[dict[str, object]] = []
            b64_skipped: list[str] = []
            if isinstance(raw_docs, list):
                for item in raw_docs:
                    if not isinstance(item, dict):
                        continue
                    name = (item.get("name") or "").strip()
                    b64 = item.get("content_b64") or ""
                    if not name or not b64:
                        continue
                    try:
                        content = base64.b64decode(b64)
                    except Exception:
                        b64_skipped.append(name)  # M1: doc corrupto → reportar
                        continue
                    doc_files.append({"name": name, "content": content})

            db = self._sessions_db_path()
            try:
                row_id, skipped = _ci.create_intake(
                    db_path=db,
                    client_id=client_raw,
                    brief_text=brief,
                    doc_files=doc_files,
                )
                all_skipped = b64_skipped + skipped
                # FIX 3: add human-readable status fields for non-technical users.
                self._json({
                    "ok": True,
                    "intake_id": row_id,
                    "skipped_docs": all_skipped,
                    "status": "pending",
                    "status_label": _intake_status_label("pending"),
                    "next_step": (
                        "Un operador revisará tu solicitud y aprobará el build. "
                        "Verás el avance en el panel Proyecto en cuanto empiece."
                    ),
                })
            except (ValueError, OSError) as exc:
                payload = json.dumps(
                    {"ok": False, "error": str(exc)[:200]},
                    ensure_ascii=False,
                ).encode()
                self._send(400, payload, "application/json; charset=utf-8")
            except Exception as exc:
                payload = json.dumps(
                    {"ok": False, "error": str(exc)[:200]},
                    ensure_ascii=False,
                ).encode()
                self._send(500, payload, "application/json; charset=utf-8")

        def _get_intakes(self, query: str) -> None:
            """GET /intakes — lista intakes, opcionalmente filtrados por ?status=<s>.

            Read-only; guarded by ``_SENSITIVE_GETS`` (same CSRF/DNS-rebinding
            check as /memory and /project).

            FIX 3: hides internal disk paths (brief_path/docs_dir); adds
            brief_preview (first ~120 chars of brief text) and status_label
            (human-readable Spanish label per status).

            Args:
                query: Query string de la URL (puede contener ``status=``).
            """
            if not _CI_OK or _ci is None:
                self._json({"available": False, "reason": "cowork_intake no disponible"})
                return
            status_filter = (parse_qs(query).get("status") or [None])[0]
            db = self._sessions_db_path()
            try:
                raw_items = _ci.list_intakes(db, status=status_filter)
                items = []
                data_dir = db.parent
                for it in raw_items:
                    # Build brief_preview from the brief file (fail-open: empty string)
                    brief_preview = ""
                    brief_rel = it.get("brief_path") or ""
                    if brief_rel:
                        try:
                            brief_preview = (data_dir / brief_rel).read_text(
                                encoding="utf-8", errors="replace"
                            )[:120]
                        except OSError:
                            pass
                    items.append({
                        "id": it["id"],
                        "client_id": it["client_id"],
                        "status": it["status"],
                        "status_label": _intake_status_label(it["status"]),
                        "brief_preview": brief_preview,
                        "created_at": it["created_at"],
                    })
                self._json({"available": True, "intakes": items})
            except Exception:
                self._json({"available": False, "reason": "error interno del lector"})

        def _post_run_intake(self, body: dict) -> None:
            """POST /run-intake — triggers build for a specific intake (operator action).

            Accepts JSON ``{intake_id: <int>}`` or ``{client: <str>}`` (uses oldest
            pending for that client).  Runs ``run_once`` in a background thread so
            the response is immediate.  The CSRF same-origin guard is applied by
            do_POST before routing.

            FIX 2: launcher is resolved from the module-level ``_INTAKE_LAUNCHER``
            which tests replace with a fake — the real claude binary is never called
            in tests.
            """
            if not _CR_OK or _cr is None:
                self._send(
                    503,
                    b'{"ok": false, "error": "cowork_runner no disponible"}',
                    "application/json; charset=utf-8",
                )
                return
            if not _CI_OK or _ci is None:
                self._send(
                    503,
                    b'{"ok": false, "error": "cowork_intake no disponible"}',
                    "application/json; charset=utf-8",
                )
                return

            db = self._sessions_db_path()

            # Resolve intake to process
            intake_id_raw = body.get("intake_id")
            client_raw = (body.get("client") or "").strip()

            intake: dict | None = None
            if intake_id_raw is not None:
                try:
                    intake = _ci.get_intake(db, int(intake_id_raw))
                except (ValueError, TypeError):
                    pass
            elif client_raw:
                pending = _ci.list_intakes(db, status="pending")
                # list_intakes returns DESC; oldest = last
                for p in reversed(pending):
                    if p.get("client_id") == client_raw:
                        intake = p
                        break

            if intake is None:
                payload = json.dumps(
                    {"ok": False, "error": "intake_id o client requerido, y el intake debe estar pending"},
                    ensure_ascii=False,
                ).encode()
                self._send(400, payload, "application/json; charset=utf-8")
                return

            if intake.get("status") != "pending":
                payload = json.dumps(
                    {"ok": False, "error": f"El intake {intake['id']} no está pending (estado: {intake.get('status')})"},
                    ensure_ascii=False,
                ).encode()
                self._send(400, payload, "application/json; charset=utf-8")
                return

            # Resolve launcher: use module-level _INTAKE_LAUNCHER (monkeypatchable in tests)
            import aris4u_console.server as _self_module
            launcher = _self_module._INTAKE_LAUNCHER
            if launcher is None:
                launcher = _cr._default_launcher

            # Bind to a local non-optional name so Pyright sees it's not None
            # inside the _run() closure (the guard above already ensured this).
            _cr_bound = _cr

            # Run in background thread so we respond immediately
            result_holder: dict = {}

            def _run() -> None:
                try:
                    result = _cr_bound.run_once(db, launcher=launcher)
                    result_holder["result"] = result
                except Exception as exc:
                    result_holder["error"] = str(exc)[:300]

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            # Give it 50 ms to fail fast (e.g. no pending intake) before responding
            t.join(timeout=0.05)

            if "error" in result_holder:
                payload = json.dumps(
                    {"ok": False, "error": result_holder["error"]},
                    ensure_ascii=False,
                ).encode()
                self._send(500, payload, "application/json; charset=utf-8")
                return

            self._json({
                "ok": True,
                "intake_id": intake["id"],
                "status": "running",
                "message": "Build iniciado. Revisa el panel Proyecto para ver el progreso.",
            })

        # --- End intake ----------------------------------------------------------

        def _amplifier_label(self, body: dict) -> None:
            """Etiqueta una llamada del amplificador (útil/no) — escritura de la cabina."""
            call_id = (body.get("call_id") or "").strip()
            if not call_id:
                self._send(400, b'{"ok":false,"error":"call_id requerido"}',
                           "application/json; charset=utf-8")
                return
            res = live_data.append_label(repo, call_id, bool(body.get("useful")),
                                         (body.get("note") or "")[:200])
            self._json({**res, "amplifier": live_data.read_amplifier(repo)})

        def _serve_telemetry_stream(self) -> None:
            """SSE en vivo del log de eventos (tail -f) — 'ver ARIS4U pensar'."""
            if not self._origin_ok():
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            path = repo / "logs" / "v16.1-events.jsonl"
            if not path.is_file():
                self._send(404, b"no event log", "text/plain; charset=utf-8")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self._tail_events(path)
            except (BrokenPipeError, ConnectionError, OSError):
                pass

        def _tail_events(self, path: Path) -> None:
            """Sigue el log desde el final y empuja cada línea nueva como evento SSE."""
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(0, 2)  # arrancar al final: solo lo NUEVO
                while True:
                    line = f.readline()
                    if not line:
                        try:  # el log_rotator del motor trunca en sitio → re-seek si encogió
                            if path.stat().st_size < f.tell():
                                f.seek(0)
                        except OSError:
                            pass
                        self._sse_write(b": ping\n\n")
                        time.sleep(1.0)
                        continue
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    payload = json.dumps({
                        "ts": ev.get("ts", ""),
                        "type": ev.get("event") or ev.get("hook") or "?",
                        "hook": ev.get("hook", ""),
                        "summary": live_data.event_summary(ev),
                    }, ensure_ascii=False)
                    self._sse_write(b"data: " + payload.encode("utf-8") + b"\n\n")

        def _mcp(self, body: dict) -> None:
            """Invoca una MCP tool de la lista blanca. Write (aris_ingest) requiere confirm."""
            tool = body.get("tool", "")
            spec = _MCP_TOOLS.get(tool)
            if spec is None:
                self._send(403, b'{"ok":false,"error":"tool no permitida"}',
                           "application/json; charset=utf-8")
                return
            # local = dispara inferencia pesada (modelo local) · write = modifica la memoria.
            # Ambas exigen confirmación explícita (RULES: el modelo local es opt-in/medido).
            if spec["kind"] in ("local", "write") and not body.get("confirm"):
                self._json({"ok": False, "need_confirm": True, "tool": tool,
                            "kind": spec["kind"]})
                return
            self._json(invoke_mcp(repo, tool, body.get("args", {}) or {}))

        def _serve_vendor(self, route: str) -> None:
            vdir = (Path(__file__).resolve().parent / "vendor").resolve()
            target = (vdir / route[len("/vendor/"):]).resolve()
            if (vdir != target and vdir not in target.parents) or not target.is_file():
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            self._send(200, target.read_bytes(),
                       _VENDOR_CTYPES.get(target.suffix, "application/octet-stream"))

        def _read_body(self, max_bytes: int = _MAX_CODE_BYTES) -> dict:
            try:
                n = int(self.headers.get("Content-Length", 0))
                # Tope: un body mayor a max_bytes abre agotamiento de memoria/thread.
                # Se lee acotado y se rechaza; el caller define el cap por ruta.
                if n > max_bytes:
                    return {}
                return json.loads(self.rfile.read(max(0, n)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return {}

        def _pty_start(self, body: dict) -> None:
            argv = _PRESETS.get(body.get("preset", "shell"))
            if argv is None:
                self._send(400, b'{"error":"preset desconocido"}', "application/json")
                return
            sid = secrets.token_hex(8)
            with _SESSIONS_LOCK:
                # Tope de sesiones vivas: sin esto, /pty/start sin abrir el stream acumula
                # procesos zsh indefinidamente (la limpieza solo ocurre al cerrar el SSE).
                if len(_SESSIONS) >= _MAX_PTY_SESSIONS:
                    self._send(429, b'{"error":"demasiadas sesiones PTY activas"}',
                               "application/json")
                    return
                _SESSIONS[sid] = PtySession(argv, str(repo))
            self._send(200, json.dumps({"id": sid}).encode(), "application/json")

        def _pty_input(self, body: dict) -> None:
            sess = _SESSIONS.get(body.get("id", ""))
            if sess is not None:
                try:
                    sess.write(base64.b64decode(body.get("data", "")))
                except (ValueError, TypeError):
                    pass
            self._send(200, b"{}", "application/json")

        def _pty_resize(self, body: dict) -> None:
            # FIX #8: int() sin try/except daba 500 si rows/cols llegaban como string inválido.
            try:
                sess = _SESSIONS.get(body.get("id", ""))
                if sess is not None:
                    rows = int(body.get("rows", 24))
                    cols = int(body.get("cols", 80))
                    sess.resize(rows, cols)
                self._send(200, b"{}", "application/json")
            except (ValueError, TypeError) as exc:
                self._send(400,
                           json.dumps({"error": f"rows/cols inválidos: {exc}"}).encode(),
                           "application/json; charset=utf-8")

        def _sse_write(self, line: bytes) -> None:
            self.wfile.write(line)
            self.wfile.flush()

        def _pump(self, sess: PtySession) -> None:
            while True:
                data = sess.read()
                if data is None:
                    if not sess.alive():
                        self._sse_write(b"event: exit\ndata: \n\n")
                        return
                    self._sse_write(b": ping\n\n")
                    continue
                if data == b"":
                    self._sse_write(b"event: exit\ndata: \n\n")
                    return
                self._sse_write(b"data: " + base64.b64encode(data) + b"\n\n")

        def _serve_pty_stream(self, query: str) -> None:
            if not self._origin_ok():
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            sid = (parse_qs(query).get("id") or [""])[0]
            sess = _SESSIONS.get(sid)
            if sess is None:
                self._send(404, b"no session", "text/plain; charset=utf-8")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                self._pump(sess)
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                sess.kill()
                with _SESSIONS_LOCK:
                    _SESSIONS.pop(sid, None)

        def do_GET(self) -> None:  # noqa: N802 (firma de la stdlib)
            parsed = urlparse(self.path)
            route = parsed.path
            # Los GET que exponen contenido del repo (/code) o memoria con secretos
            # cross-client (/memory*) deben pasar el mismo guard CSRF/DNS-rebinding que los
            # POST: si no, una página cross-origin del navegador (con DNS-rebinding) podría
            # leerlos. curl/Claude no mandan Origin → _origin_ok los permite (no son CSRF).
            if route in _SENSITIVE_GETS and not self._origin_ok():
                self._send(403, b"forbidden (cross-origin)", "text/plain; charset=utf-8")
                return
            if route in ("/", "/index.html", "/console.html"):
                self._send_file(out_dir / "console.html", "text/html; charset=utf-8")
            elif route == "/inventory.json":
                self._send_file(out_dir / "inventory.json", "application/json; charset=utf-8")
            elif route == "/manifest":
                self._manifest()
            elif route == "/code":
                self._serve_code(parsed.query)
            elif route == "/status":
                self._status()
            elif route == "/atoms":
                self._atoms()
            elif route == "/cap/skills":
                self._capability("skills")
            elif route == "/cap/agents":
                self._capability("agents")
            elif route == "/cap/mcp":
                self._capability("mcp")
            elif route == "/cap/api":
                self._capability("api")
            elif route == "/cap/test/skills":
                self._capability_test("skills")
            elif route == "/cap/test/agents":
                self._capability_test("agents")
            elif route == "/cap/test/mcp":
                self._capability_test("mcp")
            elif route == "/cap/test/api":
                self._capability_test("api")
            elif route == "/valorizacion":
                self._valorizacion()
            elif route == "/auditoria":
                self._auditoria()
            elif route == "/backlog":
                self._backlog()
            elif route == "/skeletons":
                self._skeletons()
            elif route == "/memory":
                self._memory()
            elif route == "/memory/facets":
                self._memory_facets()
            elif route == "/memory/search":
                self._memory_search(parsed.query)
            elif route == "/telemetry":
                self._telemetry(parsed.query)
            elif route == "/telemetry/stream":
                self._serve_telemetry_stream()
            elif route == "/hooks":
                self._hooks()
            elif route == "/amplifier":
                self._amplifier()
            elif route == "/routing":
                self._routing(parsed.query)
            elif route == "/config":
                self._config()
            elif route == "/quality":
                self._quality()
            elif route == "/briefs":
                self._briefs(parsed.query)
            elif route == "/phi-guard-blocks":
                self._phi_guard_blocks()
            elif route == "/project":
                self._project_timeline(parsed.query)
            elif route == "/project/stream":
                self._project_stream(parsed.query)
            elif route == "/project/comments":
                self._project_comments(parsed.query)
            elif route == "/intakes":
                self._get_intakes(parsed.query)
            elif route.startswith("/vendor/"):
                self._serve_vendor(route)
            elif route == "/pty/stream":
                self._serve_pty_stream(parsed.query)
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802 (firma de la stdlib)
            if not self._origin_ok():
                self._send(403, b'{"error":"origen no permitido (CSRF)"}',
                           "application/json; charset=utf-8")
                return
            route = urlparse(self.path).path
            # /intake puede traer docs en base64: usa un cap más alto (10 MB).
            body = self._read_body(
                max_bytes=_MAX_INTAKE_BODY_BYTES if route == "/intake" else _MAX_CODE_BYTES
            )
            if route == "/pty/start":
                self._pty_start(body)
            elif route == "/pty/input":
                self._pty_input(body)
            elif route == "/pty/resize":
                self._pty_resize(body)
            elif route == "/review":
                self._review(body)
            elif route == "/apply":
                self._apply(body)
            elif route == "/revert":
                self._revert(body)
            elif route == "/regenerate":
                regenerate(repo, out_dir)
                self._json({"ok": True})
            elif route == "/mcp":
                self._mcp(body)
            elif route == "/amplifier/label":
                self._amplifier_label(body)
            elif route == "/project/comment":
                self._project_comment(body)
            elif route == "/intake":
                self._post_intake(body)
            elif route == "/run-intake":
                self._post_run_intake(body)
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

    return Handler


def regenerate(repo: Path, out_dir: Path) -> None:
    """Regenera inventario + pantalla (siempre fresco del código)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Fix #2: invalida la caché de _project_profile antes de regenerar.
    # lru_cache(64) por proceso → sin invalidación, /regenerate puede devolver
    # present=False stale para proyectos recién creados o movidos.
    live_data._project_profile.cache_clear()
    live_data._repo_python_files.cache_clear()
    # console_repo = la propia Console (su raíz) → mapa completo: motor + wrapper + consola.
    console_root = Path(__file__).resolve().parent.parent
    inv = inventory.build_inventory(repo, console_repo=console_root)
    (out_dir / "inventory.json").write_text(
        json.dumps(inv, ensure_ascii=False, indent=2), encoding="utf-8")
    curated = json.loads(
        (Path(__file__).resolve().parent.parent / "data" / "curated_semantics.json")
        .read_text(encoding="utf-8"))
    (out_dir / "console.html").write_text(
        render_console.render_console_html(inv, curated), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Arranca el servidor local."""
    ap = argparse.ArgumentParser(description="Servidor local de la Live Console")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--repo", type=Path, default=inventory.DEFAULT_REPO)
    ap.add_argument("--no-open", action="store_true", help="no abrir el navegador")
    args = ap.parse_args(argv)

    out_dir = Path(__file__).resolve().parent.parent / "out"
    print("Regenerando inventario + pantalla del código vivo…")
    regenerate(args.repo, out_dir)

    httpd = ThreadingHTTPServer((HOST, args.port), _make_handler(args.repo, out_dir))
    httpd.daemon_threads = True  # los SSE/PTY corren en bucle: que no bloqueen el Ctrl-C
    url = f"http://{HOST}:{args.port}/"
    print(f"Live Console en {url}  (Ctrl-C para parar · bind {HOST}, read-only)")
    if not args.no_open:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
