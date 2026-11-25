"""Business error types used by every pipeline entry point."""

from __future__ import annotations

from typing import Any


class PipelineError(Exception):
    """A failure carrying a stable business code and machine-readable details."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "details": self.details,
        }


E_DUP_DOC = "E_DUP_DOC"
E_CHAR_LEN_MISMATCH = "E_CHAR_LEN_MISMATCH"
E_TERM_ENTRY_INVALID = "E_TERM_ENTRY_INVALID"
E_CONTEXT_RANGE_INVALID = "E_CONTEXT_RANGE_INVALID"
E_WEIGHT_NEGATIVE = "E_WEIGHT_NEGATIVE"
E_FEATURE_UNKNOWN = "E_FEATURE_UNKNOWN"
E_SENSE_OUT_OF_RANGE = "E_SENSE_OUT_OF_RANGE"
E_RULE_VERSION_UNKNOWN = "E_RULE_VERSION_UNKNOWN"
E_COMPARE_BUDGET = "E_COMPARE_BUDGET"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_PATH_DIVERGENCE = "E_PATH_DIVERGENCE"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_INDEX_INCONSISTENT = "E_INDEX_INCONSISTENT"

