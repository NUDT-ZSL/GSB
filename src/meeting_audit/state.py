from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, canonical_bytes, content_key


STAGES = (
    "frame_features",
    "turn_detection",
    "anomaly_classification",
    "identity_adjudication",
    "ledger_write",
    "audit_report",
)


class ContentAddressedCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits: list[str] = []
        self.misses: list[str] = []

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> Any | None:
        path = self.path_for(key)
        if path.exists():
            import json

            self.hits.append(key)
            return json.loads(path.read_text(encoding="utf-8"))
        self.misses.append(key)
        return None

    def put(self, value: Any, key: str | None = None) -> str:
        key = key or content_key(value)
        path = self.path_for(key)
        if not path.exists():
            atomic_write_json(path, value)
        return key

    def stats(self) -> dict[str, Any]:
        return {
            "hit_count": len(self.hits),
            "miss_count": len(self.misses),
            "hits": list(self.hits),
            "misses": list(self.misses),
        }


class SnapshotStore:
    """Persistent monotonically versioned stage snapshots.

    Only complete stage snapshots are committed via ``os.replace``.  ``.tmp``
    and ``pending`` data are intentionally non-authoritative and are removed on
    recovery.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.snapshot_dir = self.root / "snapshots"
        self.pending_dir = self.root / "pending"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    def recover(self) -> dict[str, list[str]]:
        discarded: list[str] = []
        for path in list(self.root.rglob("*.tmp")) + list(self.pending_dir.glob("*.json")):
            if path.exists():
                discarded.append(str(path.relative_to(self.root)))
                path.unlink()
        if self.pending_dir.exists():
            for child in self.pending_dir.iterdir():
                if child.is_file():
                    child.unlink()
        return {"discarded_intermediate_files": discarded}

    def _latest_path(self, stage: str) -> Path:
        return self.snapshot_dir / f"{stage}.latest.json"

    def latest(self, stage: str) -> dict[str, Any] | None:
        path = self._latest_path(stage)
        if not path.exists():
            return None
        import json

        return json.loads(path.read_text(encoding="utf-8"))

    def latest_all(self) -> dict[str, dict[str, Any] | None]:
        return {stage: self.latest(stage) for stage in STAGES}

    def current_version(self, stage: str) -> int:
        latest = self.latest(stage)
        return int(latest["version"]) if latest else 0

    def commit_stage(
        self,
        stage: str,
        completed_intervals: list[list[int]],
        artifact_keys: dict[str, Any],
        payload: dict[str, Any] | None = None,
        completed_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        version = self.current_version(stage) + 1
        snapshot = {
            "schema": "meeting-audit/stage-snapshot/v1",
            "stage": stage,
            "version": version,
            "committed": True,
            "completed_intervals": completed_intervals,
            "completed_ids": completed_ids or [],
            "artifact_keys": artifact_keys,
            "payload": payload or {},
            "snapshot_key": "",
        }
        snapshot["snapshot_key"] = content_key(
            {k: v for k, v in snapshot.items() if k != "snapshot_key"}
        )
        versioned = self.snapshot_dir / f"{stage}.v{version:06d}.json"
        atomic_write_json(versioned, snapshot)
        atomic_write_json(self._latest_path(stage), snapshot)
        return snapshot

    def write_fake_uncommitted(self, stage: str, payload: dict[str, Any]) -> Path:
        path = self.pending_dir / f"{stage}.pending.json"
        path.write_bytes(canonical_bytes(payload))
        tmp = self.snapshot_dir / f"{stage}.latest.json.tmp"
        tmp.write_bytes(canonical_bytes(payload))
        return path

    def reset(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
