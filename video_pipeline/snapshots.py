"""Atomic stage snapshots with semantic-configuration guards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_json, read_json
from .errors import SnapshotConfigMismatch, SnapshotFormatError


class SnapshotStore:
    STAGE_FILENAMES = {
        "detection": "detection_snapshot.json",
        "matching": "matching_snapshot.json",
    }

    def __init__(self, state_dir: str | Path, config_hash: str):
        self.state_dir = Path(state_dir)
        self.config_hash = config_hash
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, stage: str) -> Path:
        try:
            filename = self.STAGE_FILENAMES[stage]
        except KeyError as exc:
            raise ValueError(f"unknown snapshot stage: {stage}") from exc
        return self.state_dir / filename

    def load(self, stage: str) -> dict[str, Any] | None:
        path = self.path_for(stage)
        if not path.exists():
            return None
        try:
            payload = read_json(path)
        except (OSError, ValueError) as exc:
            raise SnapshotFormatError(f"cannot read {stage} snapshot: {exc}") from exc
        if not isinstance(payload, dict):
            raise SnapshotFormatError(f"{stage} snapshot must be an object")
        if payload.get("stage") != stage:
            raise SnapshotFormatError(
                f"snapshot stage mismatch: expected {stage!r}, got {payload.get('stage')!r}"
            )
        if payload.get("config_hash") != self.config_hash:
            raise SnapshotConfigMismatch(
                f"{stage} snapshot config hash does not match current semantic configuration"
            )
        if not isinstance(payload.get("routes"), dict):
            raise SnapshotFormatError(f"{stage} snapshot is missing routes")
        return payload

    def save(self, stage: str, payload: dict[str, Any]) -> None:
        document = dict(payload)
        document["schema_version"] = 1
        document["stage"] = stage
        document["config_hash"] = self.config_hash
        atomic_write_json(self.path_for(stage), document)

    def discard(self, stage: str) -> None:
        try:
            self.path_for(stage).unlink()
        except FileNotFoundError:
            pass
