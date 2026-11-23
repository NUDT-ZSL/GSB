from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    WAL_INVALID_ROW = "WAL_INVALID_ROW"
    WAL_CHAIN_BROKEN = "WAL_CHAIN_BROKEN"
    WAL_HASH_MISMATCH = "WAL_HASH_MISMATCH"
    WAL_CHECKPOINT_DEPENDENCY = "WAL_CHECKPOINT_DEPENDENCY"
    CHECKPOINT_INVALID = "CHECKPOINT_INVALID"
    CHECKPOINT_HASH_MISMATCH = "CHECKPOINT_HASH_MISMATCH"
    CHECKPOINT_CHAIN_BROKEN = "CHECKPOINT_CHAIN_BROKEN"
    CHECKPOINT_ROLLBACK = "CHECKPOINT_ROLLBACK"
    CHECKPOINT_CONFLICT = "CHECKPOINT_CONFLICT"
    CLOCK_ROLLBACK = "CLOCK_ROLLBACK"
    CLOCK_BEFORE_COMMIT = "CLOCK_BEFORE_COMMIT"
    EDIT_TARGET_NOT_FOUND = "EDIT_TARGET_NOT_FOUND"
    UNSUPPORTED_EDIT = "UNSUPPORTED_EDIT"
    REPLAY_ROW_MISSING = "REPLAY_ROW_MISSING"
    REPLAY_INCONSISTENCY = "REPLAY_INCONSISTENCY"
    STATE_NOT_INITIALIZED = "STATE_NOT_INITIALIZED"
    STATE_STALE = "STATE_STALE"
    RECOVERY_CONFLICT = "RECOVERY_CONFLICT"
    LOCK_TIMEOUT = "LOCK_TIMEOUT"
    IO_ERROR = "IO_ERROR"


@dataclass
class AppError(Exception):
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            },
        }
