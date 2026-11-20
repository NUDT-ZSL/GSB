from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_EXIT_CODES = {
    "E_BAD_SPAN": 10,
    "E_BAD_TOPOLOGY": 11,
    "E_UNKNOWN_SERVICE": 12,
    "E_MISSING_PARENT": 13,
    "E_TIME_INVERSION": 14,
    "E_ATTRIBUTE_TYPE_CONFLICT": 15,
    "E_POLICY_ROLLBACK": 16,
    "E_QUOTA_MISMATCH": 17,
    "E_CLOCK_ROLLBACK": 18,
    "E_LATE_SPAN_REJECTED": 19,
    "E_DECISION_CONFLICT": 20,
    "E_CONFIG_CONFLICT": 21,
    "E_EVENT_CONFLICT": 22,
    "E_VERIFY_FAILED": 23,
    "E_NOT_FOUND": 24,
    "E_BAD_INPUT": 25,
}


@dataclass(slots=True)
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return ERROR_EXIT_CODES.get(self.code, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def fail(code: str, message: str, **details: Any) -> None:
    raise BusinessError(code=code, message=message, details=details)

