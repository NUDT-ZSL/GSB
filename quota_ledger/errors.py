from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusinessError(Exception):
    """An input or state error that must stop processing."""

    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path:
            result["path"] = self.path
        if self.details:
            result["details"] = self.details
        return result


def fail(code: str, message: str, path: str | None = None, **details: Any) -> None:
    raise BusinessError(code=code, message=message, path=path, details=details)

