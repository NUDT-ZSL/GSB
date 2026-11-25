from __future__ import annotations

import glob
import json
import os
from typing import Any

from .cache import atomic_write_json
from .errors import WorkbenchError
from .hashing import content_hash


class CheckpointStore:
    def __init__(self, root: str, snapshot_version: str = "snapshot-v1"):
        self.root = root
        self.snapshot_version = snapshot_version
        os.makedirs(root, exist_ok=True)
        self.latest_path = os.path.join(root, "latest.json")

    def _paths(self) -> list[str]:
        return sorted(glob.glob(os.path.join(self.root, "checkpoint_*.json")))

    def latest_version(self) -> int:
        latest = self.load_latest(verify=False)
        if latest is not None:
            return int(latest["version"])
        paths = self._paths()
        if not paths:
            return 0
        return int(os.path.basename(paths[-1]).split("_")[1].split(".")[0])

    def load_latest(self, verify: bool = True) -> dict[str, Any] | None:
        if not os.path.exists(self.latest_path):
            paths = self._paths()
            if not paths:
                return None
            path = paths[-1]
        else:
            path = self.latest_path
        with open(path, "r", encoding="utf-8") as handle:
            checkpoint = json.load(handle)
        if verify:
            expected = checkpoint.get("checksum")
            payload = {k: v for k, v in checkpoint.items() if k != "checksum"}
            actual = content_hash(payload)
            if expected != actual:
                raise WorkbenchError("E_CHECKPOINT_CORRUPT", "checkpoint checksum mismatch", path=path)
        return checkpoint

    def commit(
        self,
        *,
        kind: str,
        completed_edit_ids: list[str],
        stage_state: dict[str, Any],
        edit_id: str | None = None,
        stage: str | None = None,
        batch_index: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = self.load_latest(verify=True)
        version = int(previous["version"]) + 1 if previous else self.latest_version() + 1
        payload: dict[str, Any] = {
            "snapshot_version": self.snapshot_version,
            "version": version,
            "kind": kind,
            "edit_id": edit_id,
            "stage": stage,
            "batch_index": batch_index,
            "completed_edit_ids": sorted(completed_edit_ids),
            "stage_state": stage_state,
            "parent_version": previous["version"] if previous else 0,
            "parent_checksum": previous["checksum"] if previous else None,
        }
        if extra:
            payload["extra"] = extra
        payload["checksum"] = content_hash({k: v for k, v in payload.items() if k != "checksum"})
        path = os.path.join(self.root, f"checkpoint_{version:08d}.json")
        atomic_write_json(path, payload)
        atomic_write_json(self.latest_path, payload)
        return payload

    def verify_chain(self) -> dict[str, Any]:
        paths = self._paths()
        previous_version = 0
        previous_checksum = None
        completed_sets = []
        for path in paths:
            with open(path, "r", encoding="utf-8") as handle:
                checkpoint = json.load(handle)
            checksum = checkpoint.pop("checksum", None)
            actual = content_hash(checkpoint)
            checkpoint["checksum"] = checksum
            if checksum != actual:
                raise WorkbenchError("E_CHECKPOINT_CORRUPT", "checksum mismatch", path=path)
            if int(checkpoint["version"]) != previous_version + 1:
                raise WorkbenchError("E_CHECKPOINT_CORRUPT", "non-monotonic version", path=path)
            if checkpoint["parent_checksum"] != previous_checksum:
                raise WorkbenchError("E_CHECKPOINT_CORRUPT", "broken parent hash", path=path)
            previous_version = int(checkpoint["version"])
            previous_checksum = checksum
            completed_sets.append(checkpoint["completed_edit_ids"])
        return {
            "checkpoint_count": len(paths),
            "latest_version": previous_version,
            "chain_valid": True,
            "final_completed_edit_ids": completed_sets[-1] if completed_sets else [],
        }
