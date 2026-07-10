"""Tests del servidor local — foco en el guard anti path-traversal (seguridad crítica)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aris4u_console import server  # noqa: E402


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "engine").mkdir()
    (tmp_path / "engine" / "mod.py").write_text("print('hola')\n", encoding="utf-8")
    (tmp_path / "secret.bin").write_bytes(b"\x00\x01")
    return tmp_path


def test_accepts_repo_relative_text_file(tmp_path: Path) -> None:
    """Un archivo de texto dentro del repo se resuelve correctamente."""
    repo = _repo(tmp_path)
    got = server.safe_repo_path(repo, "engine/mod.py")
    assert got is not None and got.name == "mod.py"


def test_rejects_path_traversal(tmp_path: Path) -> None:
    """`../`-traversal fuera del repo se rechaza (None)."""
    repo = _repo(tmp_path)
    assert server.safe_repo_path(repo, "../../etc/passwd") is None
    assert server.safe_repo_path(repo, "engine/../../escape.py") is None


def test_rejects_absolute_outside_repo(tmp_path: Path) -> None:
    """Una ruta absoluta fuera del repo se rechaza."""
    repo = _repo(tmp_path)
    assert server.safe_repo_path(repo, "/etc/hosts") is None


def test_rejects_non_text_and_dirs(tmp_path: Path) -> None:
    """Binarios (sufijo no permitido) y directorios se rechazan."""
    repo = _repo(tmp_path)
    assert server.safe_repo_path(repo, "secret.bin") is None
    assert server.safe_repo_path(repo, "engine") is None


def test_rejects_missing(tmp_path: Path) -> None:
    """Un archivo inexistente se rechaza."""
    repo = _repo(tmp_path)
    assert server.safe_repo_path(repo, "engine/nope.py") is None


def test_file_hash_changes_with_content(tmp_path: Path) -> None:
    """El hash (base del staleness check) cambia si el archivo cambia, y es '' si falta."""
    f = tmp_path / "x.py"
    f.write_text("a = 1\n", encoding="utf-8")
    h1 = server.file_hash(f)
    assert h1 and server.file_hash(f) == h1  # determinista
    f.write_text("a = 2\n", encoding="utf-8")
    assert server.file_hash(f) != h1
    assert server.file_hash(tmp_path / "nope.py") == ""


def test_hostname_parsing() -> None:
    """_hostname extrae el host (sin esquema ni puerto) — base del guard anti DNS-rebinding."""
    assert server._hostname("127.0.0.1:8787") == "127.0.0.1"
    assert server._hostname("http://localhost:8787/x") == "localhost"
    assert server._hostname("http://evil.com") == "evil.com"
    assert server._hostname("[::1]:8787") == "::1"
    # un dominio del atacante (DNS-rebinding) NO está en la lista de hosts locales
    assert server._hostname("http://attacker.example") not in server._LOCAL_HOSTS
    assert "127.0.0.1" in server._LOCAL_HOSTS and "localhost" in server._LOCAL_HOSTS


def test_invoke_mcp_rejects_non_whitelisted(tmp_path: Path) -> None:
    """Una tool fuera de la lista blanca se rechaza SIN tocar subprocess (defensa A3)."""
    out = server.invoke_mcp(tmp_path, "os.system", {"cmd": "rm -rf /"})
    assert out["ok"] is False and "no permitida" in out["error"]


def test_mcp_whitelist_shape() -> None:
    """La lista blanca tiene solo las 7 tools conocidas y kinds válidos."""
    assert set(server._MCP_TOOLS) == {
        "aris_health", "aris_search", "aris_recall_client", "aris_structure",
        "aris_critique", "aris_dialectic", "aris_ingest"}
    for spec in server._MCP_TOOLS.values():
        assert spec["kind"] in {"read", "local", "write"}
        assert isinstance(spec["args"], list) and isinstance(spec["timeout"], int)
    # solo aris_ingest escribe (la única que requiere confirm)
    writers = [t for t, s in server._MCP_TOOLS.items() if s["kind"] == "write"]
    assert writers == ["aris_ingest"]
