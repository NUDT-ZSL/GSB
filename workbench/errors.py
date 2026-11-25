from __future__ import annotations

from typing import Any, Dict, Optional


class WorkbenchError(Exception):
    """Business error carrying a stable diagnostic code."""

    exit_code = 2

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        exit_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        if exit_code is not None:
            self.exit_code = exit_code

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


class ResultMismatchError(WorkbenchError):
    exit_code = 20

    def __init__(self, message: str, details: Dict[str, Any]) -> None:
        super().__init__("E_RESULT_MISMATCH", message, details, exit_code=20)
