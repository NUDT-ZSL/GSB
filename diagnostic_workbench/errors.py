from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BusinessError(Exception):
    code: str
    message: str
    field_path: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    http_status: int = 400

    def __str__(self) -> str:
        location = f" at {self.field_path}" if self.field_path else ""
        return f"{self.code}: {self.message}{location}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "context": self.context,
                "field_path": self.field_path,
                "message": self.message,
            }
        }


def fail(
    code: str,
    message: str,
    *,
    field_path: str | None = None,
    context: dict[str, Any] | None = None,
    http_status: int = 400,
) -> None:
    raise BusinessError(
        code=code,
        message=message,
        field_path=field_path,
        context=context or {},
        http_status=http_status,
    )


def not_found(message: str, **context: Any) -> None:
    fail("NOT_FOUND", message, context=context, http_status=404)


def conflict(
    code: str,
    message: str,
    *,
    field_path: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    fail(
        code,
        message,
        field_path=field_path,
        context=context,
        http_status=409,
    )

