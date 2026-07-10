"""Handler PreToolUse — f5_prevalidation (advisory / shadow-mode).

Porta `hooks/f5_prevalidation.sh`: gate de calidad de output en Write/Edit. Corre el
motor F5 (`engine.v16.f5_validacion.ValidacionEngine`) con un contrato por tipo de
archivo y, si el verdict != PASS, emite un aviso (advisory). NUNCA bloquea (shadow-mode,
== el `exit 0` del .sh). Fail-open total ante cualquier error del motor.

Pura: devuelve Verdict. La selección de contrato por extensión replica el `case` del .sh.
"""
from __future__ import annotations

from dispatch.contract import ARIS4U_ROOT
from dispatch.handlers import verdict as V


def _f5_log(tool_name: str, file_path: str, fmt: str) -> None:
    """A0.6: telemetría mínima de F5 — antes invisible (0 eventos en 90k). Fail-silent."""
    try:
        import json as _j
        from datetime import datetime, UTC as _UTC
        log = ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as fh:
            fh.write(_j.dumps({
                "ts": datetime.now(_UTC).isoformat(),
                "hook": "f5_prevalidation",
                "event": "f5_gate",
                "tool": tool_name,
                "file": file_path[:120],
                "fmt": fmt,
            }) + "\n")
    except Exception:
        pass


def _contract_for(file_path: str) -> tuple[str, int]:
    """Replica el `case "$FILE_PATH"` del .sh (formato + min_length por tipo)."""
    fp = file_path
    test_markers = ("/test_", "_test.go", ".spec.ts", ".spec.tsx",
                    ".test.ts", ".test.tsx", "/tests/")
    if (fp.startswith("test_") or any(m in fp for m in test_markers)):
        return "test", 5
    if fp.endswith((".md", ".txt", ".rst", ".adoc")):
        return "docs", 10
    if fp.endswith((".json", ".yaml", ".yml", ".toml")):
        return "config", 2
    if fp.endswith((".sh", ".bash", ".zsh")):
        return "script", 10
    if fp.endswith((".py", ".dart", ".java", ".kt", ".ts", ".tsx",
                    ".js", ".jsx", ".go", ".rs", ".rb")):
        return "code", 20
    return "code", 5


def check(tool_name: str, tool_input: dict) -> V.Verdict:
    """Veredicto F5 (siempre advisory). PASS si no aplica, no hay contenido, o verdict=PASS."""
    if tool_name not in ("Write", "Edit"):
        return V.ok()
    content = (tool_input or {}).get("content") or ""
    if not content:
        return V.ok()
    file_path = (tool_input or {}).get("file_path") or ""

    fmt, min_len = _contract_for(file_path)

    _f5_log(tool_name, file_path, fmt)  # A0.6: emite evento antes del try para visibilidad

    try:
        import sys
        root = str(ARIS4U_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)
        from engine.v16.f5_validacion import ValidacionEngine  # type: ignore

        engine = ValidacionEngine()
        result = engine.validate(
            output=content,
            contract={"format": fmt, "min_length": min_len},
            context={"query": "PreToolUse gate", "temperature": 0.0},
        )
        verdict = getattr(result, "verdict", "PASS")
        if verdict == "PASS":
            return V.ok()
        issues = getattr(result, "issues", []) or []
        first = ""
        if issues:
            it = issues[0]
            first = it.get("description", "") if isinstance(it, dict) else str(it)
        return V.advise(f"F5.VALIDACION [{verdict}]: {first}")
    except Exception:
        # Shadow-mode: motor roto → no avisar, nunca bloquear.
        return V.ok()
