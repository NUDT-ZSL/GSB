"""Atomically replaced batch checkpoints with checksummed replay records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .canonical import atomic_write_json, read_json_strict_no_duplicates, sha256_json
from .errors import (
    E_CHECKPOINT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    WorkbenchError,
    corrupt,
)
from .ledger import StageLock


@dataclass(frozen=True)
class CheckpointState:
    version: int
    schedule_key: str | None
    completed_request_ids: tuple[str, ...]
    decision_records: Mapping[str, Mapping[str, Any]]
    batch_index: int
    checksum: str | None
    raw: Mapping[str, Any] | None = None

    @classmethod
    def empty(cls) -> "CheckpointState":
        return cls(
            version=0,
            schedule_key=None,
            completed_request_ids=(),
            decision_records={},
            batch_index=-1,
            checksum=None,
        )


@dataclass(frozen=True)
class CheckpointCommit:
    version: int
    checksum: str
    completed_count: int
    drift_detected: bool
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checksum": self.checksum,
            "completed_count": self.completed_count,
            "drift_detected": self.drift_detected,
            "idempotent": self.idempotent,
        }


class CheckpointStore:
    def __init__(
        self,
        root: str | Path,
        *,
        lock: StageLock | None = None,
    ):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "checkpoint.json"
        self.lock = lock or StageLock(self.root / "stage.lock")

    def read(self) -> CheckpointState:
        if not self.path.exists():
            return CheckpointState.empty()
        envelope = read_json_strict_no_duplicates(self.path)
        if not isinstance(envelope, dict):
            raise corrupt("checkpoint must be a JSON object")
        required = {
            "schema_version",
            "checkpoint_version",
            "schedule_key",
            "completed_request_ids",
            "decisions",
            "batch_index",
            "content_checksum",
        }
        missing = sorted(required.difference(envelope))
        if missing:
            raise corrupt("checkpoint envelope is incomplete", missing=missing)
        checksum = envelope.get("content_checksum")
        body = {key: value for key, value in envelope.items() if key != "content_checksum"}
        actual = sha256_json(body)
        if checksum != actual:
            raise WorkbenchError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint checksum mismatch",
                {"expected": checksum, "actual": actual, "path": str(self.path)},
            )
        version = envelope.get("checkpoint_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise corrupt("checkpoint version is invalid", version=version)
        completed = envelope.get("completed_request_ids")
        if not isinstance(completed, list) or any(
            not isinstance(item, str) or not item for item in completed
        ):
            raise corrupt("checkpoint completed_request_ids must be strings")
        completed_tuple = tuple(completed)
        if completed_tuple != tuple(sorted(set(completed_tuple))):
            raise corrupt("checkpoint request IDs must be sorted and unique")
        decisions = envelope.get("decisions")
        if not isinstance(decisions, dict):
            raise corrupt("checkpoint decisions must be an object")
        if set(decisions) != set(completed_tuple):
            raise corrupt("checkpoint completed set and decisions disagree")
        for request_id, record in decisions.items():
            if not isinstance(record, dict):
                raise corrupt("checkpoint decision record must be an object", request_id=request_id)
            if record.get("request_id") != request_id:
                raise corrupt("checkpoint decision request_id mismatch", request_id=request_id)
        batch_index = envelope.get("batch_index")
        if isinstance(batch_index, bool) or not isinstance(batch_index, int) or batch_index < 0:
            raise corrupt("checkpoint batch_index is invalid", batch_index=batch_index)
        return CheckpointState(
            version=version,
            schedule_key=str(envelope.get("schedule_key")),
            completed_request_ids=completed_tuple,
            decision_records={str(key): value for key, value in decisions.items()},
            batch_index=batch_index,
            checksum=str(checksum),
            raw=envelope,
        )

    def commit_batch(
        self,
        *,
        schedule_key: str,
        completed_request_ids: Sequence[str],
        decision_records: Mapping[str, Mapping[str, Any]],
        batch_index: int,
        expected_parent_version: int | None = None,
    ) -> CheckpointCommit:
        completed = tuple(sorted(completed_request_ids))
        if len(completed) != len(set(completed)):
            raise corrupt("checkpoint completed request IDs contain duplicates")
        if set(completed) != set(decision_records):
            raise corrupt("checkpoint decision records do not cover completed requests")
        sorted_decisions = {
            request_id: dict(decision_records[request_id]) for request_id in completed
        }
        with self.lock.hold():
            current = self.read()
            if current.schedule_key is not None and current.schedule_key != schedule_key:
                raise WorkbenchError(
                    E_CHECKPOINT_CORRUPT,
                    "checkpoint belongs to a different schedule",
                    {
                        "current_schedule_key": current.schedule_key,
                        "requested_schedule_key": schedule_key,
                    },
                )
            if batch_index < current.batch_index:
                raise WorkbenchError(
                    E_SNAPSHOT_ROLLBACK,
                    "checkpoint batch index rolled backwards",
                    {
                        "current_batch_index": current.batch_index,
                        "requested_batch_index": batch_index,
                    },
                )
            old_completed = set(current.completed_request_ids)
            if not old_completed.issubset(completed):
                raise WorkbenchError(
                    E_SNAPSHOT_ROLLBACK,
                    "new checkpoint would drop previously committed requests",
                    {
                        "current_version": current.version,
                        "missing": sorted(old_completed.difference(completed)),
                    },
                )
            if current.version > 0 and completed == current.completed_request_ids:
                current_records = {
                    key: dict(value) for key, value in current.decision_records.items()
                }
                if current_records == sorted_decisions and current.schedule_key == schedule_key:
                    return CheckpointCommit(
                        version=current.version,
                        checksum=str(current.checksum),
                        completed_count=len(completed),
                        drift_detected=False,
                        idempotent=True,
                    )
            new_version = current.version + 1
            if (
                expected_parent_version is not None
                and expected_parent_version > new_version
            ):
                raise WorkbenchError(
                    E_SNAPSHOT_ROLLBACK,
                    "checkpoint parent version is newer than the proposed commit",
                    {
                        "expected_parent_version": expected_parent_version,
                        "proposed_version": new_version,
                    },
                )
            drift = (
                expected_parent_version is not None
                and expected_parent_version != current.version
            )
            body = {
                "schema_version": 1,
                "checkpoint_version": new_version,
                "parent_version": current.version,
                "drift_detected": drift,
                "schedule_key": schedule_key,
                "batch_index": batch_index,
                "completed_request_ids": list(completed),
                "decisions": sorted_decisions,
            }
            envelope = dict(body)
            envelope["content_checksum"] = sha256_json(body)
            atomic_write_json(self.path, envelope)
            return CheckpointCommit(
                version=new_version,
                checksum=envelope["content_checksum"],
                completed_count=len(completed),
                drift_detected=drift,
            )


def schedule_key(
    requests: Sequence[Any],
    config_fingerprint: str,
) -> str:
    request_digests = sorted(
        (
            {
                "request_id": request.request_id,
                "input_digest": request.input_digest(),
            }
            for request in requests
        ),
        key=lambda item: item["request_id"],
    )
    return sha256_json(
        {
            "config_fingerprint": config_fingerprint,
            "requests": request_digests,
        }
    )

