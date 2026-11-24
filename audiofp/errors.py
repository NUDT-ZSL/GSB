from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AudioFPError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"ok": False, "error": body}


class TaskInterrupted(Exception):
    def __init__(self, exit_code: int, signal_name: str = "SIGINT") -> None:
        super().__init__(f"interrupted with exit code {exit_code}")
        self.exit_code = exit_code
        self.signal_name = signal_name


def fail(code: str, message: str, **details: Any) -> None:
    raise AudioFPError(code=code, message=message, details=details)

