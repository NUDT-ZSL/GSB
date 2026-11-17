from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXIT_CODES = {
    "OK": 0,
    "E_INTERNAL": 1,
    "E_JSON": 2,
    "E_SCHEMA": 2,
    "E_VALIDATION": 2,
    "E_CONTOUR": 2,
    "E_EDIT": 2,
    "E_PATH": 2,
    "E_IO": 3,
    "E_STATE": 3,
    "E_MEMORY": 4,
    "E_DIGEST": 5,
    "E_RENDER": 6,
    "E_INTERRUPTED": 130,
}


@dataclass
class EngineError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, EXIT_CODES["E_INTERNAL"])

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
