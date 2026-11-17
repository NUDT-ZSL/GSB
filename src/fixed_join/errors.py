from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


BUSINESS_CODES = {
    "E_TABLE",
    "E_NULLMAP",
    "E_SCALEF",
    "E_PART",
    "E_QUERY",
    "E_ATTR",
    "E_VERIFY",
    "E_IO",
}


@dataclass
class EngineError(Exception):
    code: str
    message: str
    table: str | None = None
    row: int | None = None
    column: str | None = None
    parent_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in BUSINESS_CODES:
            raise ValueError(f"unknown business code: {self.code}")
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "table": self.table,
            "row": self.row,
            "column": self.column,
        }
        if self.parent_code is not None:
            payload["parent_code"] = self.parent_code
        if self.details:
            payload["details"] = self.details
        return payload
