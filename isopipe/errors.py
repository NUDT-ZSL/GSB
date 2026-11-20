from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_EXIT_CODES = {
    "E_MANIFEST_JSON": 2,
    "E_MANIFEST_SCHEMA": 2,
    "E_DUPLICATE_TASK": 2,
    "E_PATH_ESCAPE": 2,
    "E_ENV_INVALID": 2,
    "E_TIMEOUT_INVALID": 2,
    "E_INPUT_MISSING": 2,
    "E_INPUT_INVALID": 2,
    "E_DEPENDENCY_INVALID": 2,
    "E_SANDBOX_READ_DENIED": 3,
    "E_SANDBOX_WRITE_DENIED": 4,
    "E_TASK_TIMEOUT": 5,
    "E_DIGEST_MISMATCH": 6,
    "E_CLOCK_REGRESSION": 7,
    "E_EXECUTION_DIVERGENCE": 8,
    "E_DEPENDENCY_FAILED": 9,
    "E_CACHE_CORRUPT": 10,
    "E_SANDBOX_UNAVAILABLE": 11,
    "E_LOCK_TIMEOUT": 12,
    "E_STATE_NOT_FOUND": 13,
    "E_VERIFY_FAILED": 14,
    "E_INTERNAL": 70,
}


@dataclass
class PipelineError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    @property
    def exit_code(self) -> int:
        return ERROR_EXIT_CODES.get(self.code, 70)

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


def error(code: str, message: str, **details: Any) -> PipelineError:
    return PipelineError(code=code, message=message, details=details)

