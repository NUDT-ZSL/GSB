"""Stable error codes and structured exception type."""

from __future__ import annotations

from typing import Any, Dict, Optional


class ErrorCode:
    VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
    FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
    PARSE_ERROR = "E_PARSE_ERROR"
    CHECKSUM_MISMATCH = "E_CHECKSUM_MISMATCH"
    SEMANTIC_LOSS = "E_SEMANTIC_LOSS"
    LOOP_BUDGET = "E_LOOP_BUDGET"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    FRAME_COUNT_MISMATCH = "E_FRAME_COUNT_MISMATCH"
    INVALID_CONFIG = "E_INVALID_CONFIG"
    NOT_FOUND = "E_NOT_FOUND"
    STAGE_BUSY = "E_STAGE_BUSY"


class DiagnosticError(Exception):
    """Exception carrying a stable machine-readable error code."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def error_envelope(
    code: str, message: str, details: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": {"code": code, "message": message, "details": details or {}},
    }
