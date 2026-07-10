"""F-series logger — atomic JSONL append helper with EU AI Act Art.12 hash chain.

Per V16.6 W2.1 phase design. Centralized emit pattern for novelty_detector,
schema_compat_check, migration_linter, agent_output_verifier.
contract_validator already uses this pattern (._emit_degradation).

Multiprocess-safe via fcntl.flock. Falls back to silent no-op if log dir
unwritable (instrumentation must never crash caller).

Hash chain (Batch F2, 2026-07-06):
    Each event emitted through ``emit_event()`` carries two extra fields:
    - ``prev_hash``: SHA-256 of the previous event in the chain (or
      GENESIS_HASH = '000...000' × 64 for the very first event).
    - ``hash``: SHA-256( prev_hash + canonical_json(event_without_hash_fields) )
    The chain head is persisted in ``logs/.v16-chain-head`` (64 hex chars).
    Events written directly to the log (hooks that bypass emit_event) are
    not chained — they lack ``hash``/``prev_hash`` fields and are treated as
    pre-chain by ``audit_export.py --verify``.
"""

import fcntl
import hashlib
import json
import os
from datetime import datetime, UTC
from pathlib import Path
from typing import Any


# Raíz del repo: ARIS4U_ROOT / CLAUDE_PLUGIN_ROOT si están seteadas, o derivada del
# path de este archivo (tools/_logger.py → parents[1] = raíz). Sin hardcode de usuario
# para que un tercero no loguee a un path inexistente y falle en silencio.
ARIS4U_ROOT = Path(
    os.environ.get("ARIS4U_ROOT")
    or os.environ.get("CLAUDE_PLUGIN_ROOT")
    or Path(__file__).resolve().parents[1]
)
DEFAULT_LOG = ARIS4U_ROOT / "logs" / "v16.1-events.jsonl"

# Sentinel prev_hash for the very first chained event (genesis).
_GENESIS_HASH = "0" * 64


# ---------------------------------------------------------------------------
# Hash-chain helpers
# ---------------------------------------------------------------------------


def _chain_head_read_at(chain_head: Path) -> str:
    """Return the hash of the last chained event, or GENESIS_HASH.

    Called inside ``fcntl.LOCK_EX`` so concurrent readers are serialised at
    the process level.  Falls back silently to genesis on any OS error.

    Args:
        chain_head: Path to the chain-head state file.

    Returns:
        64-char hex string (SHA-256).
    """
    try:
        h = chain_head.read_text(encoding="ascii").strip()
        if len(h) == 64 and all(c in "0123456789abcdef" for c in h):
            return h
    except OSError:
        pass
    return _GENESIS_HASH


def _chain_head_write_at(chain_head: Path, h: str) -> None:
    """Persist the hash of the latest chained event (best-effort, inside lock).

    Args:
        chain_head: Path to the chain-head state file.
        h: 64-char hex SHA-256 hash to persist.
    """
    try:
        chain_head.write_text(h + "\n", encoding="ascii")
    except OSError:
        pass


def _compute_event_hash(prev_hash: str, record_payload: dict) -> str:
    """SHA-256 of ``prev_hash + canonical_json(payload)``.

    Canonical form: JSON with sorted keys, using ``default=str`` for
    non-serialisable types (timestamps, paths).  Excludes the ``prev_hash``
    and ``hash`` fields themselves so the computation is deterministic.

    Args:
        prev_hash: 64-char hex hash of the previous event.
        record_payload: Event dict WITHOUT ``prev_hash`` / ``hash`` fields.

    Returns:
        64-char hex SHA-256 string.
    """
    canonical = json.dumps(record_payload, sort_keys=True, default=str)
    raw = f"{prev_hash}{canonical}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def emit_event(event: str, source: str, **fields: Any) -> None:
    """Atomic append a single JSONL event to events log with hash chain.

    Each event written through this function carries ``prev_hash`` and
    ``hash`` fields that form a SHA-256 append-only chain detectable by
    ``audit_export.py --verify``.  The chain starts from the first call
    after Batch F deployment (prev_hash = GENESIS_HASH = '000...000' × 64).

    Uses fcntl.flock for multiproc safety. Falls back to silent no-op if
    log dir unwritable (never crash caller).

    Chain-head file is derived at call time from the current value of
    ``DEFAULT_LOG`` so that test patches to ``DEFAULT_LOG`` keep the chain
    head isolated to the test's temp directory and don't advance the
    production chain head.

    Args:
        event: Event type identifier (e.g., 'novelty_check', 'schema_finding').
        source: Tool/module that emitted the event (e.g., 'novelty_detector').
        **fields: Additional structured fields (e.g., is_new_domain=True).
    """
    record: dict[str, Any] = {
        "ts": datetime.now(UTC).isoformat(),
        "event": event,
        "source": source,
        **fields,
    }
    try:
        target_log = DEFAULT_LOG  # read once; tests may have patched the module attr
        chain_head = target_log.parent / ".v16-chain-head"
        target_log.parent.mkdir(parents=True, exist_ok=True)
        with open(target_log, "a", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                prev_hash = _chain_head_read_at(chain_head)
                h = _compute_event_hash(prev_hash, record)
                record["prev_hash"] = prev_hash
                record["hash"] = h
                f.write(json.dumps(record, default=str) + "\n")
                _chain_head_write_at(chain_head, h)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError, ValueError):
        # Never crash caller — emit is best-effort instrumentation
        pass
