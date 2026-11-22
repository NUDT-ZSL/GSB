from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_CODES = {
    "E_BAD_SAMPLE",
    "E_BAD_TABLE",
    "E_BAD_PLAN",
    "E_BAD_STATS",
    "E_BAD_CONFIG",
    "E_BAD_BATCH",
    "E_DUPLICATE_BATCH",
    "E_OUT_OF_ORDER",
    "E_STATE_MISMATCH",
    "E_NO_STATE",
    "E_SWITCH_BUDGET",
    "E_NO_BETTER_SHAPE",
    "E_NO_DATA",
    "E_VERIFY_FAILED",
    "E_CORRUPT_STATE",
    "E_IO",
}


@dataclass
class EngineError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {self.code}")
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "error": {"code": self.code, "message": self.message, "details": self.details}}
