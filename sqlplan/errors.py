from __future__ import annotations

import copy
from enum import Enum
from typing import Any, Dict, Optional


class ErrorCode(str, Enum):
    E_BAD_JSON = "E_BAD_JSON"
    E_BAD_VIEW = "E_BAD_VIEW"
    E_DUPLICATE_VIEW = "E_DUPLICATE_VIEW"
    E_UPSTREAM_NOT_FOUND = "E_UPSTREAM_NOT_FOUND"
    E_CYCLE = "E_CYCLE"
    E_SQL_PARSE = "E_SQL_PARSE"
    E_BAD_CLOCK = "E_BAD_CLOCK"
    E_CONTENT_DIGEST_MISMATCH = "E_CONTENT_DIGEST_MISMATCH"
    E_PLAN_DIGEST_MISMATCH = "E_PLAN_DIGEST_MISMATCH"
    E_BAD_SNAPSHOT = "E_BAD_SNAPSHOT"
    E_SHARD_HASH_MISMATCH = "E_SHARD_HASH_MISMATCH"
    E_SNAPSHOT_META_MISMATCH = "E_SNAPSHOT_META_MISMATCH"
    E_GENERATION_ROLLBACK = "E_GENERATION_ROLLBACK"
    E_GENERATION_CONFLICT = "E_GENERATION_CONFLICT"
    E_COLUMN_NOT_FOUND = "E_COLUMN_NOT_FOUND"
    E_COLUMN_AMBIGUOUS = "E_COLUMN_AMBIGUOUS"
    E_VIEW_EXISTS = "E_VIEW_EXISTS"
    E_VIEW_NOT_FOUND = "E_VIEW_NOT_FOUND"
    E_VIEW_IN_USE = "E_VIEW_IN_USE"
    E_PATCH_CONFLICT = "E_PATCH_CONFLICT"
    E_BAD_PATCH = "E_BAD_PATCH"
    E_BASE_GENERATION_MISMATCH = "E_BASE_GENERATION_MISMATCH"
    E_CLOCK_REGRESSION = "E_CLOCK_REGRESSION"
    E_NOT_LOADED = "E_NOT_LOADED"
    E_LOCK_TIMEOUT = "E_LOCK_TIMEOUT"
    E_RECOVERY_CONFLICT = "E_RECOVERY_CONFLICT"
    E_STATE_CORRUPT = "E_STATE_CORRUPT"
    E_INTERNAL = "E_INTERNAL"


EXIT_CODES: Dict[ErrorCode, int] = {
    ErrorCode.E_BAD_JSON: 2,
    ErrorCode.E_BAD_VIEW: 2,
    ErrorCode.E_DUPLICATE_VIEW: 2,
    ErrorCode.E_UPSTREAM_NOT_FOUND: 2,
    ErrorCode.E_CYCLE: 2,
    ErrorCode.E_SQL_PARSE: 2,
    ErrorCode.E_BAD_CLOCK: 2,
    ErrorCode.E_CONTENT_DIGEST_MISMATCH: 2,
    ErrorCode.E_PLAN_DIGEST_MISMATCH: 2,
    ErrorCode.E_BAD_SNAPSHOT: 2,
    ErrorCode.E_SHARD_HASH_MISMATCH: 2,
    ErrorCode.E_SNAPSHOT_META_MISMATCH: 2,
    ErrorCode.E_GENERATION_ROLLBACK: 3,
    ErrorCode.E_GENERATION_CONFLICT: 3,
    ErrorCode.E_COLUMN_NOT_FOUND: 4,
    ErrorCode.E_COLUMN_AMBIGUOUS: 4,
    ErrorCode.E_VIEW_EXISTS: 4,
    ErrorCode.E_VIEW_NOT_FOUND: 4,
    ErrorCode.E_VIEW_IN_USE: 4,
    ErrorCode.E_PATCH_CONFLICT: 3,
    ErrorCode.E_BAD_PATCH: 2,
    ErrorCode.E_BASE_GENERATION_MISMATCH: 3,
    ErrorCode.E_CLOCK_REGRESSION: 3,
    ErrorCode.E_NOT_LOADED: 5,
    ErrorCode.E_LOCK_TIMEOUT: 6,
    ErrorCode.E_RECOVERY_CONFLICT: 6,
    ErrorCode.E_STATE_CORRUPT: 5,
    ErrorCode.E_INTERNAL: 7,
}


class BusinessError(Exception):
    """An expected, machine-readable application failure."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 7)

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.details:
            payload["details"] = copy.deepcopy(self.details)
        return {"ok": False, "error": payload}

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"
