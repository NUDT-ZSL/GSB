from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    INVALID_JSON = "INVALID_JSON"
    INVALID_INPUT = "INVALID_INPUT"
    DUPLICATE_VOLUME = "DUPLICATE_VOLUME"
    DUPLICATE_SNAPSHOT_ID = "DUPLICATE_SNAPSHOT_ID"
    DUPLICATE_SNAPSHOT_SEQ = "DUPLICATE_SNAPSHOT_SEQ"
    DUPLICATE_REQUEST_ID = "DUPLICATE_REQUEST_ID"
    INVALID_BLOCK_SIZE = "INVALID_BLOCK_SIZE"
    INVALID_BLOCK_COUNT = "INVALID_BLOCK_COUNT"
    INVALID_BLOCK_NUMBER = "INVALID_BLOCK_NUMBER"
    INVALID_BLOCK_DATA = "INVALID_BLOCK_DATA"
    BLOCK_RANGE_INVALID = "BLOCK_RANGE_INVALID"
    COVERAGE_MISMATCH = "COVERAGE_MISMATCH"
    DIGEST_MISMATCH = "DIGEST_MISMATCH"
    PARENT_NOT_FOUND = "PARENT_NOT_FOUND"
    PARENT_ORPHANED = "PARENT_ORPHANED"
    CLOCK_NOT_MONOTONIC = "CLOCK_NOT_MONOTONIC"
    ROLLBACK_TARGET_NOT_FOUND = "ROLLBACK_TARGET_NOT_FOUND"
    ROLLBACK_TARGET_ORPHANED = "ROLLBACK_TARGET_ORPHANED"
    ROLLBACK_STALE = "ROLLBACK_STALE"
    ROLLBACK_CONFLICT = "ROLLBACK_CONFLICT"
    REQUEST_CONFLICT = "REQUEST_CONFLICT"
    SNAPSHOT_CONFLICT = "SNAPSHOT_CONFLICT"
    STORAGE_NOT_INITIALIZED = "STORAGE_NOT_INITIALIZED"
    STORAGE_ALREADY_INITIALIZED = "STORAGE_ALREADY_INITIALIZED"
    VOLUME_MISMATCH = "VOLUME_MISMATCH"
    BLOCK_MISSING = "BLOCK_MISSING"
    BLOCK_CORRUPT = "BLOCK_CORRUPT"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    VERIFY_FAILED = "VERIFY_FAILED"
    LOCK_FAILED = "LOCK_FAILED"
    IO_ERROR = "IO_ERROR"
    UNEXPECTED = "UNEXPECTED"


@dataclass(slots=True)
class BlockStoreError(Exception):
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        if self.details:
            return f"{self.code}: {self.message} ({self.details})"
        return f"{self.code}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload
