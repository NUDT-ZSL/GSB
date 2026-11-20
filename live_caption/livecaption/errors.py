"""Business error codes for the live caption stream engine."""
from __future__ import annotations

from typing import Any, Dict

E_LINE_WIDTH = "E_LINE_WIDTH"
E_LINE_SCREEN = "E_LINE_SCREEN"
E_REWRITE_DEPTH = "E_REWRITE_DEPTH"
E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
E_VIEW_INCONSISTENT = "E_VIEW_INCONSISTENT"
E_WORD_DUP = "E_WORD_DUP"
E_WORD_MISSING = "E_WORD_MISSING"
E_TIMELINE_ORDER = "E_TIMELINE_ORDER"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"

ALL_CODES = (
    E_LINE_WIDTH, E_LINE_SCREEN, E_REWRITE_DEPTH, E_LATENCY_BUDGET,
    E_VIEW_INCONSISTENT, E_WORD_DUP, E_WORD_MISSING, E_TIMELINE_ORDER,
    E_SNAPSHOT_ROLLBACK, E_SNAPSHOT_CORRUPT, E_RESOURCE_LIMIT, E_STAGE_BUSY,
)


class BusinessError(Exception):
    """Exception carrying a stable business error code and structured details."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        if code not in ALL_CODES:
            raise ValueError(f"unknown business code: {code}")
        self.code = code
        self.details: Dict[str, Any] = details
        super().__init__(f"[{code}] {message}")

    @property
    def message(self) -> str:
        return self.args[0]

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def expect(code: str, condition: bool, message: str, **details: Any) -> None:
    if not condition:
        raise BusinessError(code, message, **details)
