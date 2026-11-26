from __future__ import annotations

from typing import Any, Dict, Optional


class ErrorCode:
    MANIFEST_INVALID = "E_MANIFEST_INVALID"
    CASE_ID_DUPLICATE = "E_CASE_ID_DUPLICATE"
    DATA_SIZE_INVALID = "E_DATA_SIZE_INVALID"
    FEATURE_DIM_INVALID = "E_FEATURE_DIM_INVALID"
    LABEL_DIM_INVALID = "E_LABEL_DIM_INVALID"
    DTYPE_UNSUPPORTED = "E_DTYPE_UNSUPPORTED"
    QUANT_PARAM_INVALID = "E_QUANT_PARAM_INVALID"
    BLOCK_PLAN_INVALID = "E_BLOCK_PLAN_INVALID"
    SAMPLE_INVALID = "E_SAMPLE_INVALID"
    SAMPLE_CASE_NOT_FOUND = "E_SAMPLE_CASE_NOT_FOUND"
    TENSOR_SHAPE_INVALID = "E_TENSOR_SHAPE_INVALID"
    DIGEST_MISMATCH = "E_DIGEST_MISMATCH"
    PATCH_INVALID = "E_PATCH_INVALID"
    PATCH_CLOCK_INVALID = "E_PATCH_CLOCK_INVALID"
    BLOCK_RANGE_OUT_OF_BOUNDS = "E_BLOCK_RANGE_OUT_OF_BOUNDS"
    PATCH_GENERATION_ROLLBACK = "E_PATCH_GENERATION_ROLLBACK"
    CLOCK_ROLLBACK_TOLERANCE = "E_CLOCK_ROLLBACK_TOLERANCE"
    V1_EFFECTIVE_SECONDS_INVALID = "E_V1_EFFECTIVE_SECONDS_INVALID"
    CONFIG_V1_INVALID = "E_CONFIG_V1_INVALID"
    CONFIG_V2_INVALID = "E_CONFIG_V2_INVALID"
    CONFIG_SHARD_HASH_MISMATCH = "E_CONFIG_SHARD_HASH_MISMATCH"
    CONFIG_GENERATION_NOT_MONOTONIC = "E_CONFIG_GENERATION_NOT_MONOTONIC"
    CONFIG_KEY_DIGEST_MISMATCH = "E_CONFIG_KEY_DIGEST_MISMATCH"
    CONFIG_METADATA_MISMATCH = "E_CONFIG_METADATA_MISMATCH"
    CONFIG_VERSION_ROLLBACK = "E_CONFIG_VERSION_ROLLBACK"
    CONFIG_VERSION_CONFLICT = "E_CONFIG_VERSION_CONFLICT"
    CONFIG_PACKAGE_NOT_FOUND = "E_CONFIG_PACKAGE_NOT_FOUND"
    STATE_CORRUPT = "E_STATE_CORRUPT"
    BLOCK_NOT_FOUND = "E_BLOCK_NOT_FOUND"
    LEASE_BUSY = "E_LEASE_BUSY"
    INJECTED_FAILURE = "E_INJECTED_FAILURE"
    COMMAND_FAILED = "E_COMMAND_FAILED"


class BizError(Exception):
    """Business error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "error": self.message,
            "details": self.details,
        }
