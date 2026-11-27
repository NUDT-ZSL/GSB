"""Stable business errors for the underwater footage pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_EXIT_CODES = {
    "E_EMPTY_FRAMES": 10,
    "E_TIMESTAMPS_NOT_MONOTONIC": 11,
    "E_TIMESTAMP_LENGTH_MISMATCH": 12,
    "E_INVALID_FRAME_SHAPE": 13,
    "E_NON_INTEGER_FRAMES": 14,
    "E_INSUFFICIENT_FRAMES": 15,
    "E_INVALID_TIMESTAMPS": 16,
    "E_INVALID_INPUT": 17,
    "E_OUTPUT_WRITE_FAILED": 20,
}


@dataclass
class PipelineError(Exception):
    """An expected, machine-readable pipeline failure.

    ``code`` is deliberately stable: callers may branch on it without parsing
    human-readable messages. The CLI also uses it as its process exit code
    offset through ``ERROR_EXIT_CODES``.
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def exit_code(self) -> int:
        return ERROR_EXIT_CODES.get(self.code, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "exit_code": self.exit_code,
            },
        }
