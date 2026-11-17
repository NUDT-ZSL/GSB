"""Iteration-state persistence, version identification and recovery.

Checkpoint layout inside the state directory:

* ``state.json``          - latest checkpoint envelope
* ``state.backup.json``   - previous same-version envelope (corruption safety)
* ``state.archive.json``  - most recent envelope from a *different* version
* ``interrupt.json``      - handled-termination marker

Every write goes through a same-directory temp file followed by
``os.replace`` (see :mod:`diff_render.io_utils`), so a crash can never leave a
half-written checkpoint that looks complete.  Each envelope carries both the
scenario version digest and an inner payload digest for tamper detection.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .digest import state_payload_digest
from .errors import DigestError
from .io_utils import atomic_write_json

STATE_NAME = "state.json"
BACKUP_NAME = "state.backup.json"
ARCHIVE_NAME = "state.archive.json"
INTERRUPT_NAME = "interrupt.json"
STATE_FORMAT = "diff-render-state-v1"


def _state_path(state_dir: str, name: str) -> str:
    return os.path.join(state_dir, name)


def build_envelope(version_digest: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "format": STATE_FORMAT,
        "version_digest": version_digest,
        "payload_digest": state_payload_digest(payload),
        "payload": payload,
    }


def _load_envelope(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            envelope = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(envelope, dict) or envelope.get("format") != STATE_FORMAT:
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    if envelope.get("payload_digest") != state_payload_digest(payload):
        return None
    return envelope


def save_checkpoint(state_dir: str, version_digest: str, payload: Dict[str, Any]) -> None:
    """Persist a completed-iteration checkpoint with version handling.

    Same version: the previous envelope moves to the backup slot first.
    Different version: the previous envelope is archived and a fresh backup
    chain starts, so an old version is never silently overwritten.
    """
    os.makedirs(state_dir, exist_ok=True)
    state_path = _state_path(state_dir, STATE_NAME)
    current = _load_envelope(state_path)
    envelope = build_envelope(version_digest, payload)

    if current is not None:
        if current.get("version_digest") == version_digest:
            # Same version: keep prior envelope as a same-version backup.
            atomic_write_json(_state_path(state_dir, BACKUP_NAME), current)
        else:
            # Version changed: archive the old version instead of overwriting.
            atomic_write_json(_state_path(state_dir, ARCHIVE_NAME), current)
            backup_path = _state_path(state_dir, BACKUP_NAME)
            if os.path.exists(backup_path):
                os.unlink(backup_path)
    elif not os.path.exists(_state_path(state_dir, BACKUP_NAME)):
        # First checkpoint for this version: seed the backup chain so the
        # current envelope always has a same-version rollback target.
        atomic_write_json(_state_path(state_dir, BACKUP_NAME), envelope)
    atomic_write_json(state_path, envelope)


def load_latest_valid(
    state_dir: str, version_digest: str
) -> (Optional[Dict[str, Any]], List[Dict[str, str]]):
    """Return ``(payload, diagnostics)`` for the most recent valid state.

    The current envelope must match ``version_digest`` and carry a valid inner
    digest.  Corrupt or mismatched states fall back to the same-version backup
    and then to the archived version; total failure raises :class:`DigestError`.
    """
    diagnostics: List[Dict[str, str]] = []
    candidates = [
        (STATE_NAME, "current"),
        (BACKUP_NAME, "same-version backup"),
        (ARCHIVE_NAME, "archived version"),
    ]
    for name, label in candidates:
        path = _state_path(state_dir, name)
        envelope = _load_envelope(path)
        if envelope is None:
            if os.path.exists(path):
                diagnostics.append({"slot": label, "status": "invalid_or_corrupt"})
            continue
        if envelope.get("version_digest") != version_digest:
            diagnostics.append({"slot": label, "status": "digest_mismatch"})
            continue
        diagnostics.append({"slot": label, "status": "restored"})
        return envelope["payload"], diagnostics

    if os.path.isdir(state_dir) and os.listdir(state_dir):
        raise DigestError(
            "no persisted state matches the current version digest; "
            "all available states were invalid or from another version",
            {"state_dir": state_dir, "diagnostics": diagnostics},
        )
    return None, diagnostics


def write_interrupt_marker(state_dir: str, signal_name: str, iteration: int) -> None:
    payload = {"signal": signal_name, "last_completed_iteration": iteration}
    atomic_write_json(_state_path(state_dir, INTERRUPT_NAME), payload)


def consume_interrupt_marker(state_dir: str) -> Optional[Dict[str, Any]]:
    """Read and remove a handled-interruption marker on a successful resume."""
    path = _state_path(state_dir, INTERRUPT_NAME)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            marker = json.load(handle)
    except (json.JSONDecodeError, OSError):
        marker = None
    os.unlink(path)
    return marker
