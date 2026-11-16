"""Stage snapshots with atomic replacement and config-version identity."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import E_CONFIG_MISMATCH, E_SNAPSHOT_CORRUPT, VadError
from .util import atomic_write_json, load_json

SNAPSHOT_NAME = "vad_snapshot.json"
SNAPSHOT_VERSION = 1


@dataclass
class Snapshot:
    stage: str
    snapshot_version: int
    config_hash: str
    manifest_hash: str
    cursor: list[str]          # stable sort keys already processed
    partial_summary: dict
    run_mode: str

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "snapshot_version": self.snapshot_version,
            "config_hash": self.config_hash,
            "manifest_hash": self.manifest_hash,
            "cursor": list(self.cursor),
            "partial_summary": self.partial_summary,
            "run_mode": self.run_mode,
        }


def write_snapshot(directory: Path, snap: Snapshot) -> Path:
    path = Path(directory) / SNAPSHOT_NAME
    atomic_write_json(path, snap.to_dict())
    return path


def read_snapshot(directory: Path) -> dict | None:
    path = Path(directory) / SNAPSHOT_NAME
    if not path.exists():
        return None
    try:
        return load_json(path)
    except Exception as exc:
        raise VadError(E_SNAPSHOT_CORRUPT, f"snapshot unreadable: {exc}") from exc


def validate_snapshot(raw: dict, config_hash: str, manifest_hash: str) -> None:
    if raw.get("snapshot_version") != SNAPSHOT_VERSION:
        raise VadError(E_CONFIG_MISMATCH,
                       f"snapshot version {raw.get('snapshot_version')} != {SNAPSHOT_VERSION}")
    if raw.get("config_hash") != config_hash:
        raise VadError(E_CONFIG_MISMATCH, "config hash differs from snapshot")
    if raw.get("manifest_hash") != manifest_hash:
        raise VadError(E_CONFIG_MISMATCH, "injection manifest hash differs from snapshot")
