"""Tests para tools/aris4u_init.py.

Cubre:
  (a) --dry-run produce JSON válido sin escribir en disco.
  (b) detect_hardware() no crashea y devuelve dict con cores >= 1.
  (c) Fusión preserva claves previas del usuario.
  (d) Idempotencia: correr 2 veces sobre el mismo dir da el mismo resultado.

Nunca toca ~/.aris4u real: todas las escrituras van a tmp_path via ARIS4U_CONFIG.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools import aris4u_init as ai

# Aliases para mantener las firmas de test dentro de 100 chars en una sola línea.
# pytest resuelve fixtures por NOMBRE de parámetro, no por tipo, así que esto es seguro.
_MP = pytest.MonkeyPatch
_CF = pytest.CaptureFixture


# ──────────────────────────────────────────────────────────────────────────────
# Helpers de fixture
# ──────────────────────────────────────────────────────────────────────────────


def _make_fake_repo(parent: Path, name: str = "fake-project") -> Path:
    """Crea un directorio con .git/ para simular un repo git.

    Args:
        parent: Directorio padre donde crear el repo.
        name: Nombre del directorio del repo.

    Returns:
        Path del repo creado.
    """
    repo = parent / name
    repo.mkdir(parents=True, exist_ok=True)
    (repo / ".git").mkdir(exist_ok=True)
    return repo


def _config_path_in(tmp: Path) -> Path:
    """Ruta de config dentro de un directorio temporal.

    Args:
        tmp: Directorio temporal de pytest.

    Returns:
        tmp/.aris4u/config.json (no toca el ~/.aris4u real).
    """
    return tmp / ".aris4u" / "config.json"


# ──────────────────────────────────────────────────────────────────────────────
# (a) --dry-run produce JSON válido sin escribir en disco
# ──────────────────────────────────────────────────────────────────────────────


def test_dry_run_valid_json_no_write(tmp_path: Path, monkeypatch: _MP, capsys: _CF) -> None:
    """dry-run imprime JSON parseable y no crea el config file."""
    config_path = _config_path_in(tmp_path)
    monkeypatch.setenv("ARIS4U_CONFIG", str(config_path))

    scan_root = tmp_path / "projects"
    scan_root.mkdir()
    _make_fake_repo(scan_root, "my-lab")

    exit_code = ai.main(["--dry-run", "--yes", "--scan-root", str(scan_root)])

    assert exit_code == 0
    # El archivo NO debe haberse creado
    assert not config_path.exists(), "dry-run no debe escribir en disco"

    # El JSON es lo primero en stdout, antes del separador ---
    captured = capsys.readouterr()
    json_text = captured.out.split("---")[0].strip()
    data: dict[str, Any] = json.loads(json_text)

    assert isinstance(data, dict)
    assert "owner" in data
    assert "hardware" in data
    assert "lab_projects" in data
    assert "clients" in data
    assert "healthcare_clients" in data
    # El lab creado debe estar detectado
    assert any(lab["dir"] == "my-lab" for lab in data["lab_projects"])


# ──────────────────────────────────────────────────────────────────────────────
# (b) detect_hardware() no crashea y devuelve dict con cores >= 1
# ──────────────────────────────────────────────────────────────────────────────


def test_hardware_detection_does_not_crash() -> None:
    """detect_hardware() devuelve dict con estructura completa y cores >= 1."""
    hw = ai.detect_hardware()

    assert isinstance(hw, dict), "detect_hardware debe devolver un dict"
    assert hw["auto_detect"] is True
    primary = hw["primary"]
    assert isinstance(primary, dict)
    assert "cores" in primary
    assert primary["cores"] >= 1, "cores debe ser al menos 1"
    assert "arch" in primary
    assert "platform" in primary
    assert "workers" in hw
    assert "dead" in hw


# ──────────────────────────────────────────────────────────────────────────────
# (c) Fusión preserva claves previas del usuario
# ──────────────────────────────────────────────────────────────────────────────


def test_merge_preserves_user_keys(tmp_path: Path, monkeypatch: _MP) -> None:
    """La segunda ejecución (sin --force) preserva claves del usuario existente."""
    config_path = _config_path_in(tmp_path)
    config_path.parent.mkdir(parents=True)

    # Config previa con claves custom del usuario
    existing: dict[str, Any] = {
        "ollama_mac_url": "http://custom-host:11434",
        "ollama_w2_url": "http://10.0.0.99:11434",
        "w2_ssh": "my-custom-w2",
        "owner": "Preserved Owner",
        "custom_key": "preserved-value",
    }
    config_path.write_text(json.dumps(existing))

    monkeypatch.setenv("ARIS4U_CONFIG", str(config_path))

    scan_root = tmp_path / "projects"
    scan_root.mkdir()

    exit_code = ai.main(["--yes", "--scan-root", str(scan_root)])
    assert exit_code == 0

    result: dict[str, Any] = json.loads(config_path.read_text())

    # Claves de usuario preservadas
    assert result.get("ollama_mac_url") == "http://custom-host:11434"
    assert result.get("ollama_w2_url") == "http://10.0.0.99:11434"
    assert result.get("w2_ssh") == "my-custom-w2"
    assert result.get("owner") == "Preserved Owner"
    assert result.get("custom_key") == "preserved-value"

    # Claves auto-detectadas siempre presentes (recomputadas)
    assert "hardware" in result
    assert result["hardware"]["auto_detect"] is True
    assert "lab_projects" in result


def test_force_ignores_existing_keys(tmp_path: Path, monkeypatch: _MP) -> None:
    """--force re-inicializa ignorando la config existente."""
    config_path = _config_path_in(tmp_path)
    config_path.parent.mkdir(parents=True)

    existing = {"owner": "Old Owner", "custom_key": "old-value"}
    config_path.write_text(json.dumps(existing))

    monkeypatch.setenv("ARIS4U_CONFIG", str(config_path))

    scan_root = tmp_path / "projects"
    scan_root.mkdir()

    exit_code = ai.main(["--yes", "--force", "--scan-root", str(scan_root)])
    assert exit_code == 0

    result: dict[str, Any] = json.loads(config_path.read_text())

    # owner se re-detecta (git config user.name o $USER), no preserva "Old Owner"
    assert result.get("owner") != "Old Owner"
    # custom_key no es parte del schema por defecto → no debe estar
    assert "custom_key" not in result


# ──────────────────────────────────────────────────────────────────────────────
# (d) Idempotencia: correr 2 veces da el mismo resultado
# ──────────────────────────────────────────────────────────────────────────────


def test_idempotency(tmp_path: Path, monkeypatch: _MP) -> None:
    """Correr init 2 veces sobre el mismo dir produce el mismo resultado estructural."""
    config_path = _config_path_in(tmp_path)
    monkeypatch.setenv("ARIS4U_CONFIG", str(config_path))

    scan_root = tmp_path / "projects"
    scan_root.mkdir()
    _make_fake_repo(scan_root, "project-alpha")

    # Primera ejecución
    assert ai.main(["--yes", "--scan-root", str(scan_root)]) == 0
    result_1: dict[str, Any] = json.loads(config_path.read_text())

    # Segunda ejecución (mismos args, misma máquina)
    assert ai.main(["--yes", "--scan-root", str(scan_root)]) == 0
    result_2: dict[str, Any] = json.loads(config_path.read_text())

    # Estructura clave idéntica en ambas ejecuciones
    assert result_1["hardware"]["primary"]["cores"] == result_2["hardware"]["primary"]["cores"]
    assert result_1["hardware"]["primary"]["arch"] == result_2["hardware"]["primary"]["arch"]
    assert len(result_1["lab_projects"]) == len(result_2["lab_projects"])
    dirs_1 = {p["dir"] for p in result_1["lab_projects"]}
    dirs_2 = {p["dir"] for p in result_2["lab_projects"]}
    assert dirs_1 == dirs_2
    assert result_1.get("ollama_mac_url") == result_2.get("ollama_mac_url")
    assert result_1.get("w2_ssh") == result_2.get("w2_ssh")


# ──────────────────────────────────────────────────────────────────────────────
# Helpers internos
# ──────────────────────────────────────────────────────────────────────────────


def test_infer_topic_client(tmp_path: Path) -> None:
    """_infer_topic devuelve 'client' para paths que contienen '03-clients'."""
    path = tmp_path / "03-clients" / "my-client"
    assert ai._infer_topic(path) == "client"


def test_infer_topic_aris_client(tmp_path: Path) -> None:
    """_infer_topic devuelve 'aris-client' si existe marcador .aris-client."""
    path = tmp_path / "my-project"
    path.mkdir()
    (path / ".aris-client").touch()
    assert ai._infer_topic(path) == "aris-client"


def test_infer_topic_lab(tmp_path: Path) -> None:
    """_infer_topic devuelve 'lab' para paths sin marcadores especiales."""
    path = tmp_path / "my-lab"
    assert ai._infer_topic(path) == "lab"


def test_display_path_replaces_home() -> None:
    """_display_path sustituye el home dir por ~."""
    home = Path.home()
    path = home / "projects" / "foo"
    result = ai._display_path(path)
    assert result.startswith("~/")
    assert str(home) not in result


def test_collect_repo_paths_deduplicates(tmp_path: Path) -> None:
    """_collect_repo_paths no incluye el mismo repo dos veces."""
    scan_root = tmp_path / "projects"
    _make_fake_repo(scan_root, "alpha")
    _make_fake_repo(scan_root, "beta")

    paths = ai._collect_repo_paths(scan_root)
    names = [p.name for p in paths]
    assert len(names) == len(set(names)), "no debe haber duplicados"
    assert "alpha" in names
    assert "beta" in names


def test_validate_config_raises_on_missing_file(tmp_path: Path) -> None:
    """validate_config lanza ValueError si el archivo no existe."""
    missing = tmp_path / "no-config.json"
    with pytest.raises(ValueError, match="Config invalida"):
        ai.validate_config(missing)


def test_write_config_sets_permissions(tmp_path: Path) -> None:
    """write_config escribe el JSON y pone permisos 0o600."""
    config_path = tmp_path / ".aris4u" / "config.json"
    data: dict[str, Any] = {"owner": "test", "hardware": {"auto_detect": True}}
    ai.write_config(data, config_path)

    assert config_path.exists()
    assert json.loads(config_path.read_text())["owner"] == "test"
    mode = config_path.stat().st_mode & 0o777
    assert mode == 0o600, f"permisos esperados 0o600, obtenidos {oct(mode)}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
