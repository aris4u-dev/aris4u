"""Registro de handlers por evento del dispatcher ARIS4U.

Se puebla conforme se migran los eventos (orden de menor riesgo primero):
SubagentStart → lifecycle → UserPromptSubmit → PostToolUse → PreToolUse.
Un evento sin handler aquí cae a passthrough() en dispatch.py (no-op).
"""
from __future__ import annotations

from typing import Dict
from collections.abc import Callable

from .session_end import handle as _session_end
from .session_start import handle as _session_start
from .stop import handle as _stop
from .post_tool_use import handle as _post_tool_use
from .pre_tool_use import handle as _pre_tool_use
from .subagent_start import handle as _subagent_start
from .user_prompt_submit import handle as _user_prompt_submit

HANDLERS: Dict[str, Callable[[str, dict], None]] = {
    "SubagentStart": _subagent_start,
    "SessionStart": _session_start,
    "SessionEnd": _session_end,
    "Stop": _stop,
    "UserPromptSubmit": _user_prompt_submit,
    "PostToolUse": _post_tool_use,
    "PreToolUse": _pre_tool_use,
}
