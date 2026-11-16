"""Error codes and structured failure records."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class WorkbenchError(Exception):
    """Base error carrying a stable machine-readable code."""

    code = "E_INTERNAL"

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.context = context


class AudioFormatError(WorkbenchError):
    code = "E_AUDIO_FORMAT"


class ConfigError(WorkbenchError):
    code = "E_CONFIG"


class CommandError(WorkbenchError):
    code = "E_COMMAND"


class BudgetPending(WorkbenchError):
    """Raised internally when the interaction budget is exhausted.

    This only marks unfinished work; the engine keeps computing to completion
    in the background.  It never claims that work was aborted.
    """

    code = "E_INTERACT_BUDGET_PENDING"


class SnapshotError(WorkbenchError):
    code = "E_SNAPSHOT"


@dataclass
class FailureRecord:
    """One isolated item failure (a frame, segment or file)."""

    identifier: str
    stage: str
    error_type: str
    code: str
    message: str
    sort_key: list[Any] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "stage": self.stage,
            "error_type": self.error_type,
            "code": self.code,
            "message": self.message,
            "sort_key": self.sort_key,
        }
