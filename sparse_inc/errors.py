"""Business error type used across the component."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .codes import EXIT_CODES, OK


@dataclass
class BusinessError(Exception):
    """A stable, structured failure that callers can branch on."""

    business_code: str
    case_id: str | None = None
    segment: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)
        if self.business_code == OK:
            raise ValueError("BusinessError cannot represent OK")

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.business_code, 1)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "business_code": self.business_code,
            "case_id": self.case_id,
            "segment": self.segment,
            "message": self.message,
        }
        result.update(self.details)
        return result


def raise_business(
    code: str,
    *,
    case_id: str | None,
    segment: str | None,
    message: str,
    **details: Any,
) -> None:
    raise BusinessError(
        business_code=code,
        case_id=case_id,
        segment=segment,
        message=message,
        details=details,
    )
