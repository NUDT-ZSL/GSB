"""Atomic stage snapshot chain with monotonic versions and checksums."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any

from .errors import (
    BusinessError,
    E_INVALID_INPUT,
    E_SNAPSHOT_CORRUPT,
    E_STAGE_BUSY,
)
from .types import STAGE_INDEX, STAGE_NAMES
from .utils import (
    atomic_write_json,
    cleanup_temp_files,
    now_ms,
    read_json,
    sha256_json,
)


class StageSnapshotStore:
    schema_version = 1

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.stages_dir = self.root / "stages"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / ".lock"
        self.stages_dir.mkdir(parents=True, exist_ok=True)
        self.removed_temp_files = cleanup_temp_files(self.root)

    def _acquire(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        fd = self.lock_path.open("a+")
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            fd.close()
            raise BusinessError(
                E_STAGE_BUSY,
                "another process owns the pipeline state lock",
                details={"state_dir": str(self.root)},
            ) from exc
        return fd  # type: ignore[return-value]

    @staticmethod
    def _release(handle: Any) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _append_event(self, event: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    event,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            handle.flush()

    @staticmethod
    def _content(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in snapshot.items()
            if key not in {"checksum", "committed_at_ms"}
        }

    def _path_for_index(self, index: int, stage: str) -> Path:
        return self.stages_dir / f"{index:02d}-{stage}.json"

    def load_snapshots(self) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        expected_parent: str | None = None
        last_version = 0
        for path in sorted(self.stages_dir.glob("*.json")):
            if path.name.startswith(".tmp-"):
                continue
            snapshot = read_json(path)
            actual = sha256_json(self._content(snapshot))
            if snapshot.get("checksum") != actual:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "stage snapshot checksum mismatch",
                    details={
                        "path": str(path),
                        "expected": snapshot.get("checksum"),
                        "actual": actual,
                    },
                )
            stage = snapshot.get("stage")
            index = snapshot.get("stage_index")
            if (
                stage not in STAGE_INDEX
                or STAGE_INDEX[stage] != index
                or path != self._path_for_index(index, stage)
            ):
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "stage snapshot name/index mismatch",
                    details={"path": str(path), "stage": stage, "index": index},
                )
            if snapshots and index != snapshots[-1]["stage_index"] + 1:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "stage snapshot chain contains a gap",
                    details={
                        "path": str(path),
                        "previous_index": snapshots[-1]["stage_index"],
                        "index": index,
                    },
                )
            version = int(snapshot["snapshot_version"])
            if version <= last_version:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "stage snapshot versions must increase monotonically",
                    details={"previous_version": last_version, "version": version},
                )
            if expected_parent != snapshot.get("parent_checksum"):
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "stage snapshot parent checksum mismatch",
                    details={
                        "path": str(path),
                        "expected_parent": expected_parent,
                        "actual_parent": snapshot.get("parent_checksum"),
                    },
                )
            completed_ids = snapshot.get("completed_sample_ids")
            if (
                not isinstance(completed_ids, list)
                or completed_ids != sorted(set(completed_ids))
            ):
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "completed_sample_ids must be sorted and unique",
                    details={"path": str(path)},
                )
            expected_parent = snapshot["checksum"]
            last_version = version
            snapshots.append(snapshot)
        return snapshots

    def last_committed(self) -> dict[str, Any] | None:
        snapshots = self.load_snapshots()
        return snapshots[-1] if snapshots else None

    def get_stage(self, stage: str) -> dict[str, Any] | None:
        for snapshot in self.load_snapshots():
            if snapshot["stage"] == stage:
                return snapshot
        return None

    def commit(
        self,
        *,
        stage: str,
        payload: dict[str, Any],
        completed_sample_ids: list[str],
        expected_sample_ids: list[str],
    ) -> dict[str, Any]:
        if stage not in STAGE_INDEX:
            raise BusinessError(
                E_INVALID_INPUT,
                f"unknown stage {stage!r}",
            )
        completed = sorted(set(completed_sample_ids))
        expected = sorted(set(expected_sample_ids))
        if completed != expected:
            completed_set = set(completed)
            expected_set = set(expected)
            raise BusinessError(
                E_INVALID_INPUT,
                "stage snapshot cannot commit before all expected samples finish",
                details={
                    "stage": stage,
                    "missing": sorted(expected_set - completed_set),
                    "unexpected": sorted(completed_set - expected_set),
                    "completed_count": len(completed),
                    "expected_count": len(expected),
                },
            )
        if len(completed) != len(completed_sample_ids):
            raise BusinessError(
                E_INVALID_INPUT,
                "completed_sample_ids contains duplicates",
                details={"stage": stage},
            )
        handle = self._acquire()
        try:
            snapshots = self.load_snapshots()
            index = STAGE_INDEX[stage]
            if snapshots:
                last = snapshots[-1]
                if index <= last["stage_index"]:
                    path = self._path_for_index(index, stage)
                    if path.exists():
                        existing = read_json(path)
                        if existing.get("payload") == payload:
                            return existing
                    raise BusinessError(
                        E_SNAPSHOT_CORRUPT,
                        "cannot overwrite a committed stage",
                        details={"stage": stage, "index": index},
                    )
                if index != last["stage_index"] + 1:
                    raise BusinessError(
                        E_SNAPSHOT_CORRUPT,
                        "stages must commit in strict order",
                        details={
                            "stage": stage,
                            "expected_index": last["stage_index"] + 1,
                        },
                    )
            elif index != 1:
                raise BusinessError(
                    E_SNAPSHOT_CORRUPT,
                    "first committed stage must be ingest",
                    details={"stage": stage},
                )
            version = int(snapshots[-1]["snapshot_version"]) + 1 if snapshots else 1
            parent_checksum = snapshots[-1]["checksum"] if snapshots else None
            snapshot = {
                "schema_version": self.schema_version,
                "stage": stage,
                "stage_index": index,
                "snapshot_version": version,
                "parent_checksum": parent_checksum,
                "completed_sample_ids": completed,
                "payload": payload,
            }
            snapshot["checksum"] = sha256_json(snapshot)
            snapshot["committed_at_ms"] = now_ms()
            path = self._path_for_index(index, stage)
            atomic_write_json(path, snapshot)
            self._append_event(
                {
                    "event": "stage_committed",
                    "at_ms": snapshot["committed_at_ms"],
                    "stage": stage,
                    "stage_index": index,
                    "snapshot_version": version,
                    "checksum": snapshot["checksum"],
                    "completed_count": len(completed),
                }
            )
            return snapshot
        finally:
            self._release(handle)

    def record_event(self, event: str, **details: Any) -> None:
        self._append_event({"event": event, "at_ms": now_ms(), **details})

    def summary(self) -> dict[str, Any]:
        snapshots = self.load_snapshots()
        return {
            "state_dir": str(self.root),
            "committed_stages": [
                {
                    "stage": snapshot["stage"],
                    "stage_index": snapshot["stage_index"],
                    "snapshot_version": snapshot["snapshot_version"],
                    "checksum": snapshot["checksum"],
                    "completed_count": len(
                        snapshot["completed_sample_ids"]
                    ),
                    "committed_at_ms": snapshot.get("committed_at_ms"),
                }
                for snapshot in snapshots
            ],
            "last_committed_stage": (
                snapshots[-1]["stage"] if snapshots else None
            ),
            "discarded_temp_files": self.removed_temp_files,
            "stage_order": list(STAGE_NAMES),
        }

