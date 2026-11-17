from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .config import ENGINE_VERSION
from .errors import SnapshotMismatch
from .util import atomic_write_json


SNAPSHOT_SCHEMA_VERSION = 1


class SnapshotStore:
    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.path = work_dir / "state.json"
        self.lock = threading.Lock()

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotMismatch(
                f"latest snapshot cannot be read: {exc}",
                details={"path": str(self.path)},
            ) from exc
        if not isinstance(payload, dict):
            raise SnapshotMismatch("latest snapshot is not a JSON object")
        return payload

    def validate(
        self,
        snapshot: dict[str, Any],
        *,
        config_hash: str,
    ) -> None:
        if snapshot.get("snapshot_schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotMismatch(
                "snapshot version is incompatible with this engine",
                details={
                    "expected": SNAPSHOT_SCHEMA_VERSION,
                    "actual": snapshot.get("snapshot_schema_version"),
                },
            )
        if snapshot.get("engine_version") != ENGINE_VERSION:
            raise SnapshotMismatch(
                "snapshot engine version is incompatible with this engine",
                details={
                    "expected": ENGINE_VERSION,
                    "actual": snapshot.get("engine_version"),
                },
            )
        if snapshot.get("config_hash") != config_hash:
            raise SnapshotMismatch(
                "snapshot config hash does not match the current acoustic configuration",
                details={
                    "expected": config_hash,
                    "actual": snapshot.get("config_hash"),
                },
            )

    def save(self, state: dict[str, Any]) -> None:
        with self.lock:
            atomic_write_json(self.path, state)


def new_snapshot_state(
    *,
    config_hash: str,
    label_hash: str,
    stage: str,
) -> dict[str, Any]:
    return {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "engine_version": ENGINE_VERSION,
        "stage": stage,
        "config_hash": config_hash,
        "label_hash": label_hash,
        "command_cursor": -1,
        "command_records": [],
        "completed_phonemes": [],
        "active_phoneme": None,
        "substate_positions": {},
        "partial_feature_summary": {},
        "cache": {
            "frame_features": {},
            "energy_frames": {},
            "concat_pcm": {},
        },
        "failures": [],
        "recovery": {
            "next_stage": stage,
            "next_substate": "VOICING_DECISION",
        },
    }
