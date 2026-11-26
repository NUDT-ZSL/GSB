from __future__ import annotations

from pathlib import Path
from typing import Any

from .ledger import SnapshotStore


def discard_uncommitted_files(store: SnapshotStore) -> list[str]:
    discarded: list[str] = []
    patterns = [
        (store.work_dir, "*.inflight"),
        (store.work_dir, "*.dispatch.json"),
        (store.staging_dir, "*.partial.json"),
        (store.staging_dir, "*.partial"),
    ]
    for directory, pattern in patterns:
        for path in directory.glob(pattern):
            if path.is_file():
                relative = str(path.relative_to(store.root))
                path.unlink()
                discarded.append(relative)

    # Remove interrupted atomic-write temporary files, never versioned checkpoints.
    for directory in (store.checkpoint_dir, store.ledger_dir, store.history_dir):
        for path in directory.glob("*.tmp-*"):
            if path.is_file():
                relative = str(path.relative_to(store.root))
                path.unlink()
                discarded.append(relative)
    return sorted(discarded)


def recover_workspace(output_dir: str | Path) -> dict[str, Any]:
    store = SnapshotStore(output_dir)
    record = store.load_latest()
    discarded = discard_uncommitted_files(store)
    if record is None:
        return {
            "has_checkpoint": False,
            "checkpoint_version": 0,
            "next_batch_no": 1,
            "committed_point_ids": [],
            "discarded_uncommitted_files": discarded,
            "snapshot": None,
        }
    payload = record["payload"]
    point_ids = [result["point_id"] for result in payload.get("results", [])]
    return {
        "has_checkpoint": True,
        "checkpoint_version": int(record["snapshot_version"]),
        "next_batch_no": int(payload.get("next_batch_no", 1)),
        "committed_point_ids": point_ids,
        "discarded_uncommitted_files": discarded,
        "snapshot": record,
    }
