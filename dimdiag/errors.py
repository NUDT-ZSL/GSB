"""Stable business error codes and structured failures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_INTERMEDIATE_DIM = "E_INTERMEDIATE_DIM"
E_PRECISION_CHANGED = "E_PRECISION_CHANGED"
E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INCONSISTENT = "E_INCONSISTENT"
E_INPUT_INVALID = "E_INPUT_INVALID"


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def config_error(field: str, message: str, **details: Any) -> BusinessError:
    payload = {"field": field, **details}
    return BusinessError(E_CONFIG_INVALID, message, payload)

