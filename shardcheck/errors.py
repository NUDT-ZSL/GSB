from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FailureRecord:
    round: int
    shard_id: str | None
    stage: str
    code: str
    exception_type: str
    message: str
    injection_id: str | None = None
    fatal: bool = True
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "shard_id": self.shard_id,
            "stage": self.stage,
            "code": self.code,
            "exception_type": self.exception_type,
            "message": self.message,
            "injection_id": self.injection_id,
            "fatal": self.fatal,
            "details": self.details,
        }


class ShardCheckError(Exception):
    code = "E_SHARDCHECK"
    stage = "unknown"

    def __init__(self, message: str, *, shard_id: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.shard_id = shard_id
        self.details = details or {}

    def failure(self, round_number: int, injection_id: str | None = None, *, fatal: bool = True) -> FailureRecord:
        return FailureRecord(
            round=round_number,
            shard_id=self.shard_id,
            stage=self.stage,
            code=self.code,
            exception_type=type(self).__name__,
            message=str(self),
            injection_id=injection_id,
            fatal=fatal,
            details=self.details,
        )


class ConfigError(ShardCheckError):
    code = "E_CONFIG_INVALID"
    stage = "config"


class ShardGenerationError(ShardCheckError):
    code = "E_SHARD_GENERATION_FAILED"
    stage = "generate"


class ShardSizeExceeded(ShardCheckError):
    code = "E_SHARD_SIZE_EXCEEDED"
    stage = "generate"


class ShardWriteError(ShardCheckError):
    code = "E_SHARD_WRITE_FAILED"
    stage = "write"


class ShardConflict(ShardCheckError):
    code = "E_SHARD_CONFLICT"
    stage = "write"


class MergeError(ShardCheckError):
    code = "E_MERGE_FAILED"
    stage = "merge"


class StorageBudgetExceeded(ShardCheckError):
    code = "E_STORAGE_BUDGET_EXCEEDED"
    stage = "budget"


class SnapshotVersionMismatch(ShardCheckError):
    code = "E_SNAPSHOT_VERSION_MISMATCH"
    stage = "restore"


class SnapshotManifestMismatch(ShardCheckError):
    code = "E_SNAPSHOT_MANIFEST_MISMATCH"
    stage = "restore"


class CheckpointCorrupt(ShardCheckError):
    code = "E_CHECKPOINT_CORRUPT"
    stage = "restore"


class PolicyError(ShardCheckError):
    code = "E_POLICY_INVALID"
    stage = "config"


class InjectedError(RuntimeError):
    """Base class for deterministic fault-injection exceptions."""


class InjectedGenerateError(InjectedError):
    pass


class InjectedWriteError(InjectedError):
    pass


class InjectedMergeError(InjectedError):
    pass


class InjectedFingerprintConflict(InjectedError):
    pass


class InjectedOccupancyError(InjectedError):
    pass


class InjectedRetentionError(InjectedError):
    pass
