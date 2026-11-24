"""Business errors with stable JSON-facing error codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


INTERRUPT_EXIT_CODE = 75


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class RenderInterrupted(Exception):
    """Raised after a graceful stop was observed at a block boundary."""

    def __init__(self, summary: dict[str, Any]) -> None:
        super().__init__("render interrupted at a block boundary")
        self.summary = summary
        self.exit_code = INTERRUPT_EXIT_CODE

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "E_INTERRUPTED",
            "message": "graceful stop observed at a block boundary",
            "details": self.summary,
        }
