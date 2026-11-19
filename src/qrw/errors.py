from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_DUP_QUERY = "E_DUP_QUERY"
E_PATTERN_EMPTY = "E_PATTERN_EMPTY"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SLOT_TYPE_RETIRED = "E_SLOT_TYPE_RETIRED"
E_INCONSISTENT = "E_INCONSISTENT"


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    line: int | None = None
    field: str | None = None
    column: int | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "line": self.line,
            "field": self.field,
            "column": self.column,
            "details": self.details,
        }


def error_payload(exc: BusinessError) -> dict[str, Any]:
    return {"ok": False, "error": exc.to_dict()}

