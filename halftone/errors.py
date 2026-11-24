from __future__ import annotations

from typing import Any, Dict, Optional


class BusinessError(Exception):
    """An expected, machine-readable processing failure."""

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
        result: Dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "error": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result


class TerminationRequested(Exception):
    """Raised at a tile boundary after a manageable termination request."""

    def __init__(self, completed_tiles: int = 0) -> None:
        super().__init__("termination requested at tile boundary")
        self.completed_tiles = completed_tiles
