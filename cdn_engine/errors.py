from __future__ import annotations

from typing import Any, Dict, List, Optional


class ErrorCode:
    INPUT_NOT_FOUND = "E_INPUT_NOT_FOUND"
    INVALID_JSON = "E_INVALID_JSON"
    INVALID_SQLITE_METADATA = "E_INVALID_SQLITE_METADATA"
    SCHEMA_VIOLATION = "E_SCHEMA_VIOLATION"
    DUPLICATE_RULE_NAME = "E_DUPLICATE_RULE_NAME"
    GENERATION_NOT_MONOTONIC = "E_GENERATION_NOT_MONOTONIC"
    INVALID_EFFECTIVE_SECONDS = "E_INVALID_EFFECTIVE_SECONDS"
    INVALID_EFFECTIVE_MS = "E_INVALID_EFFECTIVE_MS"
    DANGLING_DEPENDENCY = "E_DANGLING_DEPENDENCY"
    SHARD_HASH_MISMATCH = "E_SHARD_HASH_MISMATCH"
    INVALID_TOMBSTONE = "E_INVALID_TOMBSTONE"
    DUPLICATE_NODE = "E_DUPLICATE_NODE"
    INVALID_NODE_ADDRESS = "E_INVALID_NODE_ADDRESS"
    OBS_SEQUENCE_NOT_MONOTONIC = "E_OBS_SEQUENCE_NOT_MONOTONIC"
    OBS_DIGEST_RESULT_MISMATCH = "E_OBS_DIGEST_RESULT_MISMATCH"
    OBS_REFERENCE_UNKNOWN = "E_OBS_REFERENCE_UNKNOWN"
    CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
    NO_ACTIVE_VIEW = "E_NO_ACTIVE_VIEW"
    PATCH_INVALID = "E_PATCH_INVALID"
    PATCH_IDEMPOTENCY_CONFLICT = "E_PATCH_IDEMPOTENCY_CONFLICT"
    PACKAGE_ROLLBACK = "E_PACKAGE_ROLLBACK"
    PACKAGE_VERSION_CONFLICT = "E_PACKAGE_VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "E_IDEMPOTENCY_CONFLICT"
    WINDOW_NOT_FOUND = "E_WINDOW_NOT_FOUND"
    ORIGIN_REQUEST_FAILED = "E_ORIGIN_REQUEST_FAILED"
    COMMAND_FAILED = "E_COMMAND_FAILED"


class EngineError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
        issues: Optional[List[Dict[str, Any]]] = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.issues = issues or []
        self.exit_code = exit_code

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "ok": False,
            "error": {"code": self.code, "message": self.message},
        }
        if self.details:
            result["error"]["details"] = self.details
        if self.issues:
            result["error"]["issues"] = self.issues
        return result
