"""Atomic stage snapshots and restart validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, read_json


SNAPSHOT_SCHEMA = "recon-workbench-snapshot"
SNAPSHOT_SCHEMA_VERSION = 1
_SNAPSHOT_PATTERN = re.compile(r"^snapshot-(\d{8})\.json$")


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotMismatch(SnapshotError):
    reason: str
    latest_sequence: int
    expected_version: int
    found_version: int | None
    expected_config_hash: str
    found_config_hash: str | None


class SnapshotManager:
    def __init__(self, state_root: Path):
        self.state_root = Path(state_root)
        self.directory = self.state_root / "snapshots"
        self.directory.mkdir(parents=True, exist_ok=True)

    def _snapshot_paths(self) -> list[tuple[int, Path]]:
        entries: list[tuple[int, Path]] = []
        for path in self.directory.iterdir():
            match = _SNAPSHOT_PATTERN.match(path.name)
            if match:
                entries.append((int(match.group(1)), path))
        entries.sort(key=lambda item: item[0])
        return entries

    def latest_sequence(self) -> int:
        entries = self._snapshot_paths()
        return entries[-1][0] if entries else 0

    def load_latest(self) -> dict[str, Any] | None:
        entries = self._snapshot_paths()
        if not entries:
            return None
        # Files are atomically replaced, so the greatest sequence is the newest
        # complete snapshot. A torn temporary file is never matched by the regex.
        _, path = entries[-1]
        payload = read_json(path)
        if payload.get("snapshot_schema") != SNAPSHOT_SCHEMA:
            raise SnapshotError(f"invalid snapshot schema in {path}")
        return payload

    def load_validated(
        self,
        *,
        snapshot_version: int,
        config_hash: str,
    ) -> dict[str, Any] | None:
        snapshot = self.load_latest()
        if snapshot is None:
            return None
        found_schema = snapshot.get("snapshot_schema_version")
        found_version = snapshot.get("snapshot_version")
        found_config_hash = snapshot.get("config_hash")
        if found_schema != SNAPSHOT_SCHEMA_VERSION:
            raise SnapshotMismatch(
                reason="snapshot schema version mismatch",
                latest_sequence=int(snapshot.get("sequence", 0)),
                expected_version=SNAPSHOT_SCHEMA_VERSION,
                found_version=found_schema,
                expected_config_hash=config_hash,
                found_config_hash=found_config_hash,
            )
        if found_version != snapshot_version:
            raise SnapshotMismatch(
                reason="snapshot version mismatch",
                latest_sequence=int(snapshot.get("sequence", 0)),
                expected_version=snapshot_version,
                found_version=found_version,
                expected_config_hash=config_hash,
                found_config_hash=found_config_hash,
            )
        if found_config_hash != config_hash:
            raise SnapshotMismatch(
                reason="configuration hash mismatch",
                latest_sequence=int(snapshot.get("sequence", 0)),
                expected_version=snapshot_version,
                found_version=found_version,
                expected_config_hash=config_hash,
                found_config_hash=found_config_hash,
            )
        return snapshot

    def write(
        self,
        *,
        snapshot_version: int,
        config_hash: str,
        camera_hash: str,
        input_hash: str,
        injection_hash: str,
        stage: str,
        stage_complete: bool,
        progress: dict[str, Any],
        completed_views: list[int],
        partial_pointcloud_summary: dict[str, Any],
    ) -> dict[str, Any]:
        sequence = self.latest_sequence() + 1
        snapshot: dict[str, Any] = {
            "snapshot_schema": SNAPSHOT_SCHEMA,
            "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
            "snapshot_version": snapshot_version,
            "version": snapshot_version,
            "sequence": sequence,
            "stage": stage,
            "stage_complete": stage_complete,
            "config_hash": config_hash,
            "camera_hash": camera_hash,
            "input_hash": input_hash,
            "injection_hash": injection_hash,
            "completed_views": sorted(set(int(value) for value in completed_views)),
            "progress": progress,
            "partial_pointcloud_summary": partial_pointcloud_summary,
        }
        path = self.directory / f"snapshot-{sequence:08d}.json"
        atomic_write_json(path, snapshot)
        return snapshot


def empty_progress() -> dict[str, Any]:
    return {
        "features": {"completed_keys": [], "completed_views": []},
        "matching": {"completed_keys": [], "completed_views": []},
        "depth": {"completed_keys": [], "completed_views": []},
        "merge": {"completed_keys": [], "completed_views": []},
    }


def merge_progress(saved: dict[str, Any] | None) -> dict[str, Any]:
    progress = empty_progress()
    if not saved:
        return progress
    source = saved.get("progress", {})
    for stage in progress:
        stage_source = source.get(stage, {})
        if isinstance(stage_source, dict):
            progress[stage]["completed_keys"] = sorted(
                set(str(value) for value in stage_source.get("completed_keys", []))
            )
            progress[stage]["completed_views"] = sorted(
                set(int(value) for value in stage_source.get("completed_views", []))
            )
    return progress
