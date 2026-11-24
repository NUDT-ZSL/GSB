"""Business errors and stable process exit codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXIT_CODES = {
    "E_ARGUMENT": 2,
    "E_JSON": 2,
    "E_SCHEMA": 2,
    "E_MEDIA": 2,
    "E_MEDIA_SIZE": 2,
    "E_EXTINCTION": 2,
    "E_ALBEDO": 2,
    "E_IOR": 2,
    "E_LIGHT": 2,
    "E_LIGHT_POS": 2,
    "E_LIGHT_INTENSITY": 2,
    "E_FOV": 2,
    "E_ASPECT": 2,
    "E_FIXED_SCALE": 2,
    "E_RESOLUTION": 2,
    "E_SAMPLES": 2,
    "E_STEP": 2,
    "E_MAX_STEPS": 2,
    "E_CUTOFF": 2,
    "E_TOLERANCE": 2,
    "E_TILE": 2,
    "E_CONCURRENCY": 2,
    "E_MEMORY_BUDGET": 2,
    "E_JITTER": 2,
    "E_PATH": 2,
    "E_DIGEST": 3,
    "E_IO": 4,
    "E_STATE": 4,
    "E_OUTPUT": 4,
    "E_INTERRUPTED": 130,
    "E_INTERNAL": 4,
}


@dataclass
class EngineError(Exception):
    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
        }
        if self.path:
            payload["path"] = self.path
        if self.details:
            payload["details"] = self.details
        return payload

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 2)


def error(code: str, message: str, path: str | None = None, **details: Any) -> EngineError:
    return EngineError(code=code, message=message, path=path, details=details)

