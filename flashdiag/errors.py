"""Stable business error codes used by the component and CLI."""

from __future__ import annotations

from typing import Any


E_INPUT_INVALID = "E_INPUT_INVALID"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_MISSING = "E_SNAPSHOT_MISSING"
E_LEDGER_INTEGRITY = "E_LEDGER_INTEGRITY"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"


class DiagnosticError(Exception):
    """An expected domain failure with a machine-readable business code."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def input_error(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError(E_INPUT_INVALID, message, **details)

