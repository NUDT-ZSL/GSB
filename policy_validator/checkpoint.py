"""Checkpoint persistence and crash recovery (requirement 6).

Checkpoints are written after every completed task via atomic rename.
Recovery rules:
  * only the last *complete* checkpoint is usable (a torn write simply
    leaves the previous checkpoint in place, thanks to atomic rename);
  * a structure-version mismatch (E4001) or a policy fingerprint change
    (E4002) forces a full rebuild of the batch - old task results are
    never mixed with new policy declarations.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import errors
from .storage import locked_atomic_write_json

STRUCTURE_VERSION = 1

REQUIRED_KEYS = ("structure_version", "policy_fingerprint", "batch_id",
                 "completed", "budget_consumed")


class CheckpointStore:
    def __init__(self, workdir: Path, batch_id: str, policy_fingerprint: str):
        self.path = Path(workdir) / "checkpoint.json"
        self.batch_id = batch_id
        self.policy_fingerprint = policy_fingerprint

    def initial_state(self) -> dict[str, Any]:
        return {
            "structure_version": STRUCTURE_VERSION,
            "policy_fingerprint": self.policy_fingerprint,
            "batch_id": self.batch_id,
            "completed": {},          # task_id -> TaskOutcome dict
            "budget_consumed": {"mem_bytes": 0},
        }

    def save(self, state: dict[str, Any]) -> None:
        locked_atomic_write_json(self.path, state, required_keys=REQUIRED_KEYS)

    def load_raw(self) -> dict[str, Any] | None:
        """Load the checkpoint if it is present and structurally complete."""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or any(k not in data for k in REQUIRED_KEYS):
            return None
        return data

    def recover(self) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
        """Return (state, events). ``state`` is None when the batch must be
        rebuilt from scratch; ``events`` explains why."""
        events: list[dict[str, str]] = []
        data = self.load_raw()
        if data is None:
            return None, events
        if data["structure_version"] != STRUCTURE_VERSION:
            events.append({
                "code": errors.E_CHECKPOINT_VERSION_MISMATCH,
                "message": "checkpoint structure version mismatch; rebuilding batch",
            })
            return None, events
        if data["policy_fingerprint"] != self.policy_fingerprint:
            events.append({
                "code": errors.E_POLICY_VERSION_CHANGED,
                "message": "policy declarations changed since checkpoint; rebuilding batch",
            })
            return None, events
        return data, events
