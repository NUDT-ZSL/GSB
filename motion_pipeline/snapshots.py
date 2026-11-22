"""Atomic, checksummed stage snapshots with rollback and lock protection."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from .deterministic import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_bytes,
    checksum_without,
    content_hash,
    read_json_file,
)
from .errors import BusinessError


STAGES = (
    {"index": 0, "name": "frame_sample"},
    {"index": 1, "name": "motion_encoding"},
    {"index": 2, "name": "temporal_extrapolation"},
    {"index": 3, "name": "result_aggregation"},
)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sorted_ids(value: Any, stage: str, version: int) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot clip id set is malformed",
            stage=stage,
            version=version,
            field="completed_clip_ids",
        )
    result = list(value)
    if result != sorted(result) or len(result) != len(set(result)):
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot clip ids must be unique and sorted",
            stage=stage,
            version=version,
            field="completed_clip_ids",
        )
    return result


def _validate_snapshot_dict(snapshot: Any) -> None:
    if not isinstance(snapshot, dict):
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot root must be a JSON object",
            stage="unknown",
            version=None,
        )
    version = snapshot.get("version")
    stage = snapshot.get("committed_stage", "unknown")
    if not _integer(version) or version < 1:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot version must be a positive integer",
            stage=stage,
            version=version if _integer(version) else None,
        )
    stored_checksum = snapshot.get("snapshot_checksum")
    if not isinstance(stored_checksum, str):
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot checksum is missing",
            stage=stage,
            version=version,
            field="snapshot_checksum",
        )
    if snapshot.get("kind") != "motion-pipeline-checkpoint":
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot kind is not recognized",
            stage=stage,
            version=version,
            field="kind",
        )
    if not _integer(snapshot.get("schema_version")):
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot schema_version is invalid",
            stage=stage,
            version=version,
            field="schema_version",
        )
    task_clip_ids = _sorted_ids(snapshot.get("task_clip_ids"), stage, version)
    stages = snapshot.get("stages")
    if not isinstance(stages, list) or not stages:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot has no committed stages",
            stage=stage,
            version=version,
            field="stages",
        )
    if len(stages) != version:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot version must equal the number of committed stages",
            stage=stage,
            version=version,
            field="version",
            detail={"stage_count": len(stages)},
        )
    committed_index = snapshot.get("committed_stage_index")
    if not _integer(committed_index) or committed_index != version - 1:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "committed stage index does not match snapshot version",
            stage=stage,
            version=version,
            field="committed_stage_index",
        )
    if stage != STAGES[committed_index]["name"]:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "committed stage name and index disagree",
            stage=stage,
            version=version,
            field="committed_stage",
        )
    _sorted_ids(snapshot.get("completed_clip_ids"), stage, version)
    if snapshot["completed_clip_ids"] != task_clip_ids:
        raise BusinessError(
            "E_PARTIAL_COMMIT",
            "committed stage does not contain every task clip",
            stage=stage,
            version=version,
            field="completed_clip_ids",
        )
    expected_cursor = {
        "stage_index": version,
        "stage": (
            STAGES[version]["name"] if version < len(STAGES) else "complete"
        ),
        "clip_index": 0,
    }
    if snapshot.get("cursor") != expected_cursor:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot cursor does not identify the next committed boundary",
            stage=stage,
            version=version,
            field="cursor",
        )
    for index, record in enumerate(stages):
        if not isinstance(record, dict):
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "stage record must be an object",
                stage=STAGES[index]["name"],
                version=index + 1,
                field="stages",
            )
        record_stage = record.get("stage")
        record_version = record.get("version")
        if record_stage != STAGES[index]["name"]:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "stage record name and position disagree",
                stage=record_stage if isinstance(record_stage, str) else STAGES[index]["name"],
                version=record_version if _integer(record_version) else index + 1,
                field="stage",
            )
        if not _integer(record_version) or record_version != index + 1:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "stage versions must increase monotonically by one",
                stage=STAGES[index]["name"],
                version=record_version if _integer(record_version) else index + 1,
                field="version",
            )
        if record.get("stage_index") != index:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "stage index and record position disagree",
                stage=STAGES[index]["name"],
                version=record_version,
                field="stage_index",
            )
        record_ids = _sorted_ids(
            record.get("completed_clip_ids"), record_stage, record_version
        )
        if record_ids != task_clip_ids:
            raise BusinessError(
                "E_PARTIAL_COMMIT",
                "a committed stage does not contain every task clip",
                stage=record_stage,
                version=record_version,
                field="completed_clip_ids",
                detail={"expected": task_clip_ids, "actual": record_ids},
            )
        if record.get("all_clips_complete") is not True:
            raise BusinessError(
                "E_PARTIAL_COMMIT",
                "a partially completed stage was marked as committed",
                stage=record_stage,
                version=record_version,
                field="all_clips_complete",
            )
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "stage payload must be an object",
                stage=record_stage,
                version=record_version,
                field="payload",
            )
        payload_checksum = record.get("content_checksum")
        if not isinstance(payload_checksum, str):
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "stage content checksum is missing",
                stage=record_stage,
                version=record_version,
                field="content_checksum",
            )
        actual_payload_checksum = content_hash(payload)
        if actual_payload_checksum != payload_checksum:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "stage payload checksum does not match",
                stage=record_stage,
                version=record_version,
                field="content_checksum",
                detail={
                    "stored": payload_checksum,
                    "calculated": actual_payload_checksum,
                },
            )
        if payload.get("clip_ids") != task_clip_ids:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "stage payload clip ids disagree with the snapshot",
                stage=record_stage,
                version=record_version,
                field="payload.clip_ids",
            )
        expected_stage_cursor = {
            "stage_index": index + 1,
            "stage": (
                STAGES[index + 1]["name"]
                if index + 1 < len(STAGES)
                else "complete"
            ),
            "clip_index": 0,
        }
        if record.get("cursor") != expected_stage_cursor:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "stage cursor is inconsistent",
                stage=record_stage,
                version=record_version,
                field="cursor",
            )
    history = snapshot.get("version_history")
    expected_history = [
        {
            "stage": record["stage"],
            "version": record["version"],
            "content_checksum": record["content_checksum"],
            "completed_clip_ids": record["completed_clip_ids"],
        }
        for record in stages
    ]
    if history != expected_history:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot version history is inconsistent",
            stage=stage,
            version=version,
            field="version_history",
        )
    calculated_checksum = checksum_without(snapshot, "snapshot_checksum")
    if calculated_checksum != stored_checksum:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT",
            "snapshot content checksum does not match the committed checksum",
            stage=stage,
            version=version,
            field="snapshot_checksum",
            detail={
                "stored": stored_checksum,
                "calculated": calculated_checksum,
            },
        )


class SnapshotStore:
    """A single whole-file checkpoint replaced atomically after each stage."""

    def __init__(self, workdir: str | Path) -> None:
        self.workdir = Path(workdir)
        self.snapshot_path = self.workdir / "snapshots" / "checkpoint.json"
        self.ledger_path = self.workdir / "snapshots" / "committed_ledger.json"
        self.lock_path = self.workdir / "snapshots" / ".commit.lock"
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _commit_lock(self, stage: str) -> Iterator[None]:
        handle = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BusinessError(
                    "E_STAGE_BUSY",
                    "another stage commit currently holds the commit lock",
                    stage=stage,
                ) from exc
            yield
        finally:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            finally:
                os.close(handle)

    def _read_ledger(self) -> Optional[dict[str, Any]]:
        if not self.ledger_path.exists():
            return None
        try:
            ledger = read_json_file(self.ledger_path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "committed version ledger is not parseable",
                stage="committed_ledger",
                version=None,
                detail={"error": str(exc)},
            ) from exc
        if (
            not isinstance(ledger, dict)
            or not _integer(ledger.get("version"))
            or ledger.get("version") < 1
            or not isinstance(ledger.get("snapshot_checksum"), str)
        ):
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "committed version ledger is malformed",
                stage="committed_ledger",
                version=ledger.get("version") if isinstance(ledger, dict) else None,
            )
        return ledger

    def _write_ledger(self, snapshot: dict[str, Any]) -> None:
        atomic_write_json(
            self.ledger_path,
            {
                "schema_version": 1,
                "kind": "motion-pipeline-version-ledger",
                "version": snapshot["version"],
                "committed_stage": snapshot["committed_stage"],
                "snapshot_checksum": snapshot["snapshot_checksum"],
            },
        )

    def _load_unlocked(self, *, repair_ledger: bool) -> Optional[dict[str, Any]]:
        if not self.snapshot_path.exists():
            ledger = self._read_ledger()
            if ledger is not None:
                raise BusinessError(
                    "E_SNAPSHOT_CORRUPT",
                    "version ledger exists but the committed snapshot is missing",
                    stage=ledger.get("committed_stage", "unknown"),
                    version=ledger.get("version"),
                    field="checkpoint",
                )
            return None
        try:
            raw = self.snapshot_path.read_bytes()
        except OSError as exc:
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "committed snapshot could not be read",
                stage="unknown",
                version=None,
                detail={"error": str(exc)},
            ) from exc
        try:
            snapshot = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            ledger = self._read_ledger()
            raise BusinessError(
                "E_SNAPSHOT_CORRUPT",
                "committed snapshot is not parseable",
                stage=(
                    ledger.get("committed_stage", "unknown")
                    if ledger is not None
                    else "unknown"
                ),
                version=ledger.get("version") if ledger is not None else None,
                detail={"error": str(exc)},
            ) from exc
        _validate_snapshot_dict(snapshot)
        ledger = self._read_ledger()
        if ledger is None:
            if repair_ledger:
                self._write_ledger(snapshot)
            return snapshot
        if ledger["version"] > snapshot["version"]:
            raise BusinessError(
                "E_SNAPSHOT_ROLLBACK",
                "the on-disk snapshot is older than the committed version ledger",
                stage=snapshot.get("committed_stage", "unknown"),
                version=snapshot["version"],
                detail={
                    "committed_version": ledger["version"],
                    "found_version": snapshot["version"],
                    "committed_checksum": ledger["snapshot_checksum"],
                    "found_checksum": snapshot["snapshot_checksum"],
                },
            )
        if ledger["version"] == snapshot["version"]:
            if ledger["snapshot_checksum"] != snapshot["snapshot_checksum"]:
                raise BusinessError(
                    "E_SNAPSHOT_CORRUPT",
                    "snapshot checksum differs from the committed version ledger",
                    stage=snapshot.get("committed_stage", "unknown"),
                    version=snapshot["version"],
                    detail={
                        "committed_checksum": ledger["snapshot_checksum"],
                        "found_checksum": snapshot["snapshot_checksum"],
                    },
                )
        elif repair_ledger:
            # A crash can occur after the atomic snapshot replace but before
            # the ledger replace. The complete snapshot is authoritative.
            self._write_ledger(snapshot)
        return snapshot

    def load(self) -> Optional[dict[str, Any]]:
        with self._commit_lock("load"):
            return self._load_unlocked(repair_ledger=True)

    def reset(self) -> None:
        with self._commit_lock("reset"):
            self.snapshot_path.unlink(missing_ok=True)
            self.ledger_path.unlink(missing_ok=True)

    def commit_stage(
        self,
        stage_index: int,
        payload: dict[str, Any],
        *,
        required_clip_ids: list[str],
        expected_version: Optional[int] = None,
    ) -> dict[str, Any]:
        """Commit one complete stage; partial clip sets are rejected."""

        if not _integer(stage_index) or not 0 <= stage_index < len(STAGES):
            raise BusinessError(
                "E_PARTIAL_COMMIT",
                "stage index is outside the four-stage state machine",
                stage="unknown",
                detail={"stage_index": stage_index},
            )
        stage_name = STAGES[stage_index]["name"]
        required = sorted(required_clip_ids)
        if len(required) != len(set(required)) or not required:
            raise BusinessError(
                "E_PARTIAL_COMMIT",
                "required clip id set is empty or duplicated",
                stage=stage_name,
                field="required_clip_ids",
            )
        with self._commit_lock(stage_name):
            current = self._load_unlocked(repair_ledger=False)
            if current is None:
                if stage_index != 0:
                    raise BusinessError(
                        "E_PARTIAL_COMMIT",
                        "the first committed stage must be frame_sample",
                        stage=stage_name,
                        field="stage_index",
                    )
                task_clip_ids = required
                next_version = 1
            else:
                current_version = current["version"]
                task_clip_ids = current["task_clip_ids"]
                if required != task_clip_ids:
                    raise BusinessError(
                        "E_RECOVERY_MISMATCH",
                        "task clip ids do not match the committed checkpoint",
                        stage=current["committed_stage"],
                        version=current_version,
                        field="clip_ids",
                        detail={
                            "committed": task_clip_ids,
                            "requested": required,
                        },
                    )
                if expected_version is None:
                    raise BusinessError(
                        "E_STAGE_BUSY",
                        "a concurrent or unversioned submitter found an existing checkpoint",
                        stage=STAGES[
                            min(stage_index, len(STAGES) - 1)
                        ]["name"],
                        version=current_version,
                        detail={"expected_version": None},
                    )
                if expected_version is not None and expected_version != current_version:
                    raise BusinessError(
                        "E_STAGE_BUSY",
                        "another commit advanced the checkpoint before this one",
                        stage=STAGES[
                            min(stage_index, len(STAGES) - 1)
                        ]["name"],
                        version=current_version,
                        detail={
                            "expected_version": expected_version,
                            "actual_version": current_version,
                        },
                    )
                if stage_index <= current["committed_stage_index"]:
                    raise BusinessError(
                        "E_SNAPSHOT_ROLLBACK",
                        "an already committed stage cannot be overwritten",
                        stage=stage_name,
                        version=current_version,
                    )
                if stage_index != current["committed_stage_index"] + 1:
                    raise BusinessError(
                        "E_PARTIAL_COMMIT",
                        "stages must be committed in order without gaps",
                        stage=stage_name,
                        version=current_version,
                        detail={
                            "expected_stage_index": current[
                                "committed_stage_index"
                            ]
                            + 1,
                            "requested_stage_index": stage_index,
                        },
                    )
                next_version = current_version + 1
            completed = _sorted_ids(required, stage_name, next_version)
            if completed != task_clip_ids:
                missing = sorted(set(task_clip_ids) - set(completed))
                raise BusinessError(
                    "E_PARTIAL_COMMIT",
                    "stage cannot be committed before every clip completes",
                    stage=stage_name,
                    version=next_version,
                    field="completed_clip_ids",
                    detail={
                        "missing": missing,
                        "expected": task_clip_ids,
                        "requested": completed,
                    },
                )
            payload_copy = payload
            if not isinstance(payload_copy, dict):
                raise BusinessError(
                    "E_PARTIAL_COMMIT",
                    "stage payload must be an object",
                    stage=stage_name,
                    field="payload",
                )
            if payload_copy.get("clip_ids") != task_clip_ids:
                raise BusinessError(
                    "E_PARTIAL_COMMIT",
                    "stage payload does not include every completed clip",
                    stage=stage_name,
                    field="payload.clip_ids",
                    detail={
                        "expected": task_clip_ids,
                        "actual": payload_copy.get("clip_ids"),
                    },
                )
            stage_cursor = {
                "stage_index": stage_index + 1,
                "stage": (
                    STAGES[stage_index + 1]["name"]
                    if stage_index + 1 < len(STAGES)
                    else "complete"
                ),
                "clip_index": 0,
            }
            stage_record = {
                "stage": stage_name,
                "stage_index": stage_index,
                "version": next_version,
                "completed_clip_ids": completed,
                "all_clips_complete": True,
                "content_checksum": content_hash(payload_copy),
                "payload": payload_copy,
                "cursor": stage_cursor,
            }
            stages = list(current["stages"]) if current is not None else []
            stages.append(stage_record)
            history = [
                {
                    "stage": record["stage"],
                    "version": record["version"],
                    "content_checksum": record["content_checksum"],
                    "completed_clip_ids": record["completed_clip_ids"],
                }
                for record in stages
            ]
            top_cursor = {
                "stage_index": stage_index + 1,
                "stage": (
                    STAGES[stage_index + 1]["name"]
                    if stage_index + 1 < len(STAGES)
                    else "complete"
                ),
                "clip_index": 0,
            }
            snapshot: dict[str, Any] = {
                "schema_version": 1,
                "kind": "motion-pipeline-checkpoint",
                "version": next_version,
                "committed_stage": stage_name,
                "committed_stage_index": stage_index,
                "task_clip_ids": task_clip_ids,
                "completed_clip_ids": completed,
                "cursor": top_cursor,
                "stages": stages,
                "version_history": history,
            }
            snapshot["snapshot_checksum"] = checksum_without(
                snapshot, "snapshot_checksum"
            )
            _validate_snapshot_dict(snapshot)
            data = canonical_bytes(snapshot) + b"\n"
            atomic_write_bytes(self.snapshot_path, data)
            self._write_ledger(snapshot)
            return snapshot

    def replace_snapshot(
        self,
        candidate: dict[str, Any],
        *,
        expected_version: Optional[int] = None,
    ) -> dict[str, Any]:
        """Atomically replace a complete snapshot.

        Same-version content changes are allowed only through this explicit
        whole-snapshot operation. Older versions are always rejected.
        """

        _validate_snapshot_dict(candidate)
        stage_label = candidate.get("committed_stage", "unknown")
        candidate_version = candidate["version"]
        with self._commit_lock(str(stage_label)):
            current = self._load_unlocked(repair_ledger=False)
            if current is not None:
                if expected_version is not None and current["version"] != expected_version:
                    raise BusinessError(
                        "E_STAGE_BUSY",
                        "another commit advanced the checkpoint before replacement",
                        stage=stage_label,
                        version=current["version"],
                        detail={
                            "expected_version": expected_version,
                            "actual_version": current["version"],
                        },
                    )
                if candidate_version < current["version"]:
                    raise BusinessError(
                        "E_SNAPSHOT_ROLLBACK",
                        "an older snapshot cannot replace a newer committed version",
                        stage=current["committed_stage"],
                        version=current["version"],
                        detail={
                            "committed_version": current["version"],
                            "candidate_version": candidate_version,
                        },
                    )
                if candidate_version > current["version"]:
                    for index, prefix_stage in enumerate(current["stages"]):
                        candidate_stage = candidate["stages"][index]
                        if (
                            candidate_stage.get("stage") != prefix_stage["stage"]
                            or candidate_stage.get("version")
                            != prefix_stage["version"]
                            or candidate_stage.get("content_checksum")
                            != prefix_stage["content_checksum"]
                            or candidate_stage.get("completed_clip_ids")
                            != prefix_stage["completed_clip_ids"]
                        ):
                            raise BusinessError(
                                "E_RECOVERY_MISMATCH",
                                "replacement would alter an already committed stage",
                                stage=prefix_stage["stage"],
                                version=prefix_stage["version"],
                            )
                if candidate_version == current["version"]:
                    if (
                        candidate["snapshot_checksum"]
                        == current["snapshot_checksum"]
                    ):
                        return current
            data = canonical_bytes(candidate) + b"\n"
            atomic_write_bytes(self.snapshot_path, data)
            self._write_ledger(candidate)
            return candidate


def resign_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Recompute all checksums after an intentional in-memory test mutation."""

    for index, record in enumerate(snapshot["stages"]):
        record["content_checksum"] = content_hash(record["payload"])
        record["version"] = index + 1
    snapshot["version_history"] = [
        {
            "stage": record["stage"],
            "version": record["version"],
            "content_checksum": record["content_checksum"],
            "completed_clip_ids": record["completed_clip_ids"],
        }
        for record in snapshot["stages"]
    ]
    snapshot["version"] = len(snapshot["stages"])
    snapshot["committed_stage_index"] = snapshot["version"] - 1
    snapshot["committed_stage"] = STAGES[snapshot["version"] - 1]["name"]
    snapshot["cursor"] = {
        "stage_index": snapshot["version"],
        "stage": (
            STAGES[snapshot["version"]]["name"]
            if snapshot["version"] < len(STAGES)
            else "complete"
        ),
        "clip_index": 0,
    }
    snapshot["snapshot_checksum"] = checksum_without(
        snapshot, "snapshot_checksum"
    )
    _validate_snapshot_dict(snapshot)
    return snapshot
