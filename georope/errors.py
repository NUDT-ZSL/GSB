"""Business errors returned by the local encoding service."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    locations: list[dict[str, Any]] = field(default_factory=list)
    http_status: int = 422

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "locations": self.locations,
            },
        }


def invalid_input(message: str, location: dict[str, Any] | None = None) -> BusinessError:
    return BusinessError(
        "INPUT_INVALID",
        message,
        [location] if location else [],
        400,
    )
