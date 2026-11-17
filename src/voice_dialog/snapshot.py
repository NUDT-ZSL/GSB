"""Stage snapshots committed via temp-file + os.replace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .constants import E_CONFIG_MISMATCH, SNAPSHOT_SCHEMA_VERSION
from .util import atomic_write_json, load_json, stable_hash


class SnapshotManager:
    def __init__(
        self,
        root: Path,
        *,
        config_hashes: dict[str, str],
        injection_fingerprint: str,
        read_enabled: bool = True,
        write_enabled: bool = True,
    ):
        self.root = root
        self.config_hashes = config_hashes
        self.injection_fingerprint = injection_fingerprint
        self.read_enabled = read_enabled
        self.write_enabled = write_enabled
        self.errors: list[dict[str, Any]] = []
        self.committed: dict[str, dict[str, Any]] = {}

    def _path(self, stage: str) -> Path:
        return self.root / f"{stage}.json"

    def load(self, stage: str) -> dict[str, Any] | None:
        if not self.read_enabled:
            return None
        path = self._path(stage)
        if not path.exists():
            return None
        try:
            snapshot = load_json(path)
        except (OSError, ValueError) as exc:
            self.errors.append(
                {
                    "stage": stage,
                    "code": E_CONFIG_MISMATCH,
                    "reason": f"unreadable snapshot: {type(exc).__name__}",
                }
            )
            return None
        reason = ""
        if snapshot.get("snapshot_version") != SNAPSHOT_SCHEMA_VERSION:
            reason = "snapshot_version_mismatch"
        elif snapshot.get("config_hash") != self.config_hashes.get(stage):
            reason = "config_hash_mismatch"
        elif snapshot.get("injection_hash") != self.injection_fingerprint:
            reason = "injection_hash_mismatch"
        elif snapshot.get("stage") != stage:
            reason = "stage_mismatch"
        if reason:
            self.errors.append(
                {
                    "stage": stage,
                    "code": E_CONFIG_MISMATCH,
                    "reason": reason,
                    "snapshot_version": snapshot.get("snapshot_version"),
                }
            )
            return None
        return snapshot

    def save(
        self,
        stage: str,
        *,
        processed: list[str],
        artifact_digest: str,
        cursor: int | None = None,
        status: str = "committed",
    ) -> dict[str, Any]:
        ordered = sorted(set(processed), key=lambda value: (value.lower(), value))
        snapshot = {
            "snapshot_version": SNAPSHOT_SCHEMA_VERSION,
            "stage": stage,
            "config_hash": self.config_hashes.get(stage, stable_hash({})),
            "injection_hash": self.injection_fingerprint,
            "processed_cursor": len(ordered) if cursor is None else int(cursor),
            "cursor": ordered[-1] if ordered else "",
            "processed": ordered,
            "artifact_digest": artifact_digest,
            "status": status,
        }
        self.committed[stage] = snapshot
        if self.write_enabled:
            atomic_write_json(self._path(stage), snapshot)
        return snapshot

    def summary(self) -> dict[str, Any]:
        return {
            "versions": {
                stage: self.committed.get(stage, {}).get("snapshot_version", SNAPSHOT_SCHEMA_VERSION)
                for stage in ("features", "scores", "fusion")
            },
            "committed": {
                stage: self.committed[stage]
                for stage in ("features", "scores", "fusion")
                if stage in self.committed
            },
            "errors": sorted(
                self.errors,
                key=lambda item: (item.get("stage", ""), item.get("code", ""), item.get("reason", "")),
            ),
        }
