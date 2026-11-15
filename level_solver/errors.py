"""Business errors and process exit codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXIT_OK = 0
EXIT_INPUT = 2
EXIT_CONFLICT = 3
EXIT_DIGEST = 4
EXIT_INTERNAL = 70
EXIT_INTERRUPTED = 75

_EXIT_BY_CODE = {
    "E_CONFLICT": EXIT_CONFLICT,
    "E_DIGEST": EXIT_DIGEST,
    "E_INTERRUPTED": EXIT_INTERRUPTED,
    "E_INTERNAL": EXIT_INTERNAL,
    "E_IO": EXIT_INTERNAL,
}


@dataclass
class SolverError(Exception):
    """A stable, machine-readable failure.

    Every public failure path uses an ``E_xxx`` code.  ``details`` is kept
    JSON-serializable so the CLI can directly emit it.
    """

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    @property
    def exit_code(self) -> int:
        return _EXIT_BY_CODE.get(self.code, EXIT_INPUT)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


def input_error(code: str, message: str, **details: Any) -> SolverError:
    return SolverError(code=code, message=message, details=details)
