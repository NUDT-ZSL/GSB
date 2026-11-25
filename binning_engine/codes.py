"""Structured business codes and the engine exception type."""

from __future__ import annotations

from typing import Any, Dict, Optional


class ErrorCode:
    OK = "OK"
    E_INVALID_INPUT = "E_INVALID_INPUT"
    E_JSON_PARSE = "E_JSON_PARSE"
    E_DUP_INPUT = "E_DUP_INPUT"
    E_MISSING_FIELD = "E_MISSING_FIELD"
    E_TYPE_ERROR = "E_TYPE_ERROR"
    E_NAN_VALUE = "E_NAN_VALUE"
    E_NON_FINITE_VALUE = "E_NON_FINITE_VALUE"
    E_CATEGORY_VIOLATION = "E_CATEGORY_VIOLATION"
    E_CONFIG_INVALID = "E_CONFIG_INVALID"
    E_CONFIG_VERSION = "E_CONFIG_VERSION"
    E_BIN_METHOD = "E_BIN_METHOD"
    E_CODE_METHOD = "E_CODE_METHOD"
    E_QUANT_PARAM = "E_QUANT_PARAM"
    E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
    E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    E_CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
    E_MIGRATION = "E_MIGRATION"
    E_SELF_CHECK = "E_SELF_CHECK"
    E_WORKER = "E_WORKER"
    E_FORCED_TERMINATION = "E_FORCED_TERMINATION"


class EngineError(Exception):
    """An exception carrying a stable structured business code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        *,
        row_id: Any = None,
        batch_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.row_id = row_id
        self.batch_id = batch_id

    def to_dict(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            error["details"] = self.details
        if self.row_id is not None:
            error["row_id"] = self.row_id
        if self.batch_id is not None:
            error["batch_id"] = self.batch_id
        return error


def failure_result(error: EngineError) -> Dict[str, Any]:
    return {"ok": False, "error": error.to_dict()}
