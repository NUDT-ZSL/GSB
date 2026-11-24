from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkbenchError(Exception):
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


class InputError(WorkbenchError):
    pass


class DomainError(WorkbenchError):
    pass


class RuntimeTaskError(WorkbenchError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_TASK", message, details or {}, exit_code=5)


class ReplayError(WorkbenchError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_REPLAY", message, details or {}, exit_code=3)


class DigestError(WorkbenchError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_DIGEST", message, details or {}, exit_code=4)


class BudgetError(WorkbenchError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_BUDGET", message, details or {}, exit_code=2)


class Interrupted(WorkbenchError):
    def __init__(
        self,
        signal_name: str,
        boundary: str,
        *,
        frame_index: int | None,
        scene_id: str | None,
        exit_code: int,
        test_injected: bool = False,
    ) -> None:
        super().__init__(
            "E_INTERRUPTED",
            f"interrupt handled at {boundary} boundary",
            {
                "signal": signal_name,
                "boundary": boundary,
                "frame_index": frame_index,
                "scene_id": scene_id,
                "test_injected": test_injected,
            },
            exit_code=exit_code,
        )
        self.signal_name = signal_name
        self.boundary = boundary
        self.frame_index = frame_index
        self.scene_id = scene_id
        self.test_injected = test_injected

