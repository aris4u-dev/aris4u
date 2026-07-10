"""Regresión: PHI off-by-default (2026-06-22). El gate _is_healthcare_ctx solo se
activa por 3 acciones EXPLÍCITAS; nunca por texto del prompt / env de cliente / bridge.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
if str(HOOKS) not in sys.path:
    sys.path.insert(0, str(HOOKS))

from dispatch.handlers import phi_guard as pg  # type: ignore[import-not-found]  # noqa: E402
from dispatch.handlers import verdict as V  # type: ignore[import-not-found]  # noqa: E402

_LAB1 = "/Users/x/projects/lab-project-1"
_CLIENT_C = "/Users/x/projects/client-c/inventory-system"
_CLIENT_E = "/Users/x/projects/client-e-financial"  # finance — NOT healthcare
_ARIS4U = "/Users/x/projects/aris4u"  # repo del motor — NO healthcare


@pytest.fixture(autouse=True)
def phi_guard_env_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARIS4U_HEALTHCARE", raising=False)
    monkeypatch.delenv("ARIS4U_CLIENT", raising=False)
    # CI hermeticity: _HEALTHCARE_PATH_MARKERS is empty when ~/.aris4u/config.json is absent.
    # Inject the canonical client-c markers so test_cwd_inside_client_activates passes in CI.
    monkeypatch.setattr(pg, "_HEALTHCARE_PATH_MARKERS", ("client-c", "/client-c/"))


def test_off_by_default() -> None:
    assert pg._is_healthcare_ctx(_LAB1, "") is False


def test_text_mention_does_not_activate() -> None:
    # False positive: mentioning client-c/client-a in a command must NOT activate.
    assert pg._is_healthcare_ctx(_LAB1, "grep -r client-c /var/log/client-a/x") is False


def test_client_env_does_not_activate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_CLIENT", "client-c")
    assert pg._is_healthcare_ctx(_LAB1, "") is False


def test_master_switch_activates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    assert pg._is_healthcare_ctx(_LAB1, "") is True


def test_cwd_inside_client_activates() -> None:
    assert pg._is_healthcare_ctx(_CLIENT_C, "") is True


def test_marker_in_exact_cwd_activates_not_inherited(tmp_path: Path) -> None:
    (tmp_path / ".aris-healthcare").touch()
    assert pg._is_healthcare_ctx(str(tmp_path), "") is True
    sub = tmp_path / "sub"
    sub.mkdir()
    # No se hereda subiendo el árbol → subdir sin marker propio = OFF.
    assert pg._is_healthcare_ctx(str(sub), "") is False


# ─────────────────────────────────────────────────────────────────────────────
# REGRESIÓN: FP commit con keywords regulatorios en proyecto NO-healthcare
# Documented scenario: financial platform blocked because its
# mensaje de commit contenía "HIPAA" — corregido 2026-06-24 con la precisión
# del egress-check (local commands never leak PHI) + gate _is_healthcare_ctx
# que nunca activa por texto del prompt.
# ─────────────────────────────────────────────────────────────────────────────


def test_fp1_hipaa_keyword_in_commit_non_healthcare_passes() -> None:
    """FP-1: git commit with "HIPAA compliance" in cwd client-e (non-healthcare) → PASS.

    Regresión del FP documentado: el guard bloqueaba commits de plataformas
    financieras con keywords regulatorios (HIPAA, compliance). Fuera de contexto
    healthcare el guard es no-op — el tipo de proyecto determina el contexto,
    no las palabras en el mensaje.
    """
    result = pg.check(
        "Bash",
        {"command": "git commit -m 'HIPAA compliance check for client-e financial audit'"},
        _CLIENT_E,
    )
    assert result.kind == V.PASS, (
        f"FP-1: commit con keyword HIPAA en proyecto no-healthcare debe pasar; "
        f"got kind={result.kind!r}"
    )


def test_fp2_healthcare_on_phi_keyword_egress_external_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """FP-2: ARIS4U_HEALTHCARE=1 + PHI keyword + WebFetch a URL externa → BLOCK.

    Valida el comportamiento CORRECTO (no un FP): en healthcare, datos PHI que
    egresan a una API externa SÍ se bloquean. Complementa FP-1 mostrando que el
    guard discrimina por contexto, no por presencia de keywords.
    """
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    # Aislar _log_audit: apuntar ARIS4U_ROOT a tmp sin logs/ → _log_audit es no-op.
    monkeypatch.setattr(pg, "ARIS4U_ROOT", tmp_path)

    result = pg.check(
        "WebFetch",
        {"url": "https://api.external-llm.com/v1/chat", "prompt": "hipaa patient record info"},
        _CLIENT_C,
    )
    assert result.kind == V.BLOCK, (
        "FP-2: WebFetch con PHI keyword en healthcare hacia URL externa debe ser bloqueado"
    )


def test_fp3_phi_guard_filename_in_path_non_healthcare_passes() -> None:
    """FP-3: Bash que toca 'phi_sanitizer.py' en cwd no-healthcare → PASS.

    'phi_sanitizer' en un path de archivo no coincide con ningún patrón en
    _PHI_PATTERNS (no existe \\bphi\\b standalone) y el cwd no es healthcare.
    Regresión para prevenir que nombres de módulos internos de ARIS4U disparen
    el guard accidentalmente.
    """
    result = pg.check(
        "Bash",
        {"command": "ruff check hooks/phi_sanitizer.py --fix"},
        _ARIS4U,
    )
    assert result.kind == V.PASS, (
        "FP-3: edición de phi_sanitizer.py en cwd no-healthcare no debe disparar el guard"
    )


def test_fp4_healthcare_cwd_no_phi_keyword_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FP-4: Operación rutinaria en cwd healthcare sin keyword PHI → PASS.

    El guard solo bloquea cuando hay un patrón PHI real en el texto. Comandos
    de desarrollo normal (git push, deploys, lectura de config) dentro de un
    proyecto healthcare no deben ser afectados.
    """
    monkeypatch.setenv("ARIS4U_HEALTHCARE", "1")
    result = pg.check(
        "Bash",
        {"command": "git push origin main --force-with-lease"},
        _CLIENT_C,
    )
    assert result.kind == V.PASS, (
        "FP-4: comando sin PHI en cwd healthcare debe pasar; solo el PHI real se bloquea"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
