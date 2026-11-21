"""Business error codes used by the diagnostic tool."""

from __future__ import annotations


class DiagnosticError(Exception):
    """An error with a stable machine-readable business code."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


E_NO_TABLE = "E_NO_TABLE"
E_NO_COLUMN = "E_NO_COLUMN"
E_DUP_TABLE = "E_DUP_TABLE"
E_DUP_INDEX = "E_DUP_INDEX"
E_DUP_TX = "E_DUP_TX"
E_BAD_JSON = "E_BAD_JSON"
E_BAD_OP = "E_BAD_OP"
E_BAD_OPERATION_ORDER = "E_BAD_OPERATION_ORDER"
E_BAD_BUDGET = "E_BAD_BUDGET"
E_BAD_CONCURRENCY = "E_BAD_CONCURRENCY"
E_BAD_SCALE = "E_BAD_SCALE"
E_BAD_TOLERANCE = "E_BAD_TOLERANCE"
E_BAD_LOCK_MODE = "E_BAD_LOCK_MODE"
E_BAD_ISOLATION = "E_BAD_ISOLATION"
E_BAD_CLOCK = "E_BAD_CLOCK"
E_BAD_DEPENDENCY = "E_BAD_DEPENDENCY"
E_TX_CYCLE = "E_TX_CYCLE"
E_SAMPLE_MISMATCH = "E_SAMPLE_MISMATCH"
E_STATE_EXISTS = "E_STATE_EXISTS"
E_NO_STATE = "E_NO_STATE"
E_STATE_CORRUPT = "E_STATE_CORRUPT"
E_INTERNAL = "E_INTERNAL"
