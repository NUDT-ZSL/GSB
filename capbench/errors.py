from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class ValidationError(WorkbenchError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "$",
        value: Any = None,
    ) -> None:
        details: dict[str, Any] = {"path": path}
        if value is not None:
            details["value"] = value
        super().__init__(code, message, details)


def err(
    code: str,
    message: str,
    *,
    path: str = "$",
    value: Any = None,
) -> ValidationError:
    return ValidationError(code, message, path=path, value=value)

