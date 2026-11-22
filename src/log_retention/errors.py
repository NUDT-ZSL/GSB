from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ArchiveError(Exception):
    """A stable business error surfaced by the command line interface."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class InjectedCrash(RuntimeError):
    """Used only by tests to emulate SIGKILL between durable stages."""


def fail(code: str, message: str, **details: Any) -> None:
    raise ArchiveError(code=code, message=message, details=details)
