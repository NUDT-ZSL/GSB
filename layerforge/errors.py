from __future__ import annotations

from typing import Any, Dict, Optional


class ErrorCode:
    PATH_ESCAPE = "E_PATH_ESCAPE"
    BASE_LAYER_MISSING = "E_BASE_LAYER_MISSING"
    GENERATION_ROLLBACK = "E_GENERATION_ROLLBACK"
    SHARD_HASH_MISMATCH = "E_SHARD_HASH_MISMATCH"
    SAMPLE_DIGEST_CONFLICT = "E_SAMPLE_DIGEST_CONFLICT"
    CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
    LAYER_CONFLICT = "E_LAYER_CONFLICT"
    VALIDATION = "E_VALIDATION"
    VERSION_CONFLICT = "E_VERSION_CONFLICT"
    LOCK_BUSY = "E_LOCK_BUSY"
    OBJECT_CORRUPT = "E_OBJECT_CORRUPT"
    BUDGET_ABANDONED = "E_BUDGET_ABANDONED"
    WORKER_FAILED = "E_WORKER_FAILED"


EXIT_CODES = {
    ErrorCode.PATH_ESCAPE: 20,
    ErrorCode.BASE_LAYER_MISSING: 21,
    ErrorCode.GENERATION_ROLLBACK: 22,
    ErrorCode.SHARD_HASH_MISMATCH: 23,
    ErrorCode.SAMPLE_DIGEST_CONFLICT: 24,
    ErrorCode.CLOCK_ROLLBACK: 25,
    ErrorCode.LAYER_CONFLICT: 26,
    ErrorCode.VALIDATION: 27,
    ErrorCode.VERSION_CONFLICT: 28,
    ErrorCode.LOCK_BUSY: 29,
    ErrorCode.BUDGET_ABANDONED: 30,
    ErrorCode.WORKER_FAILED: 31,
    ErrorCode.OBJECT_CORRUPT: 32,
}


class LayerForgeError(Exception):
    """Business exception carrying a stable diagnostic code."""

    code = ErrorCode.VALIDATION

    def __init__(
        self,
        message: str,
        *,
        details: Optional[Dict[str, Any]] = None,
        code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "details": self.details,
        }

    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 2)


class PathEscapeError(LayerForgeError):
    code = ErrorCode.PATH_ESCAPE


class BaseLayerMissingError(LayerForgeError):
    code = ErrorCode.BASE_LAYER_MISSING


class GenerationRollbackError(LayerForgeError):
    code = ErrorCode.GENERATION_ROLLBACK


class ShardHashMismatchError(LayerForgeError):
    code = ErrorCode.SHARD_HASH_MISMATCH


class SampleDigestConflictError(LayerForgeError):
    code = ErrorCode.SAMPLE_DIGEST_CONFLICT


class ClockRollbackError(LayerForgeError):
    code = ErrorCode.CLOCK_ROLLBACK


class LayerConflictError(LayerForgeError):
    code = ErrorCode.LAYER_CONFLICT
