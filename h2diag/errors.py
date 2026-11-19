from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "details": self.details,
            "message": self.message,
        }


class ScenarioError(WorkbenchError):
    pass


class EngineError(WorkbenchError):
    pass


class StorageError(WorkbenchError):
    pass


class VerificationError(WorkbenchError):
    pass


E_BAD_SCENARIO = "E_BAD_SCENARIO"
E_UNKNOWN_STREAM = "E_UNKNOWN_STREAM"
E_STREAM_STATE = "E_STREAM_STATE"
E_FLOW_CONTROL = "E_FLOW_CONTROL"
E_WINDOW_OVERFLOW = "E_WINDOW_OVERFLOW"
E_HEADERS_SEQUENCE = "E_HEADERS_SEQUENCE"
E_HEADERS_INCOMPLETE = "E_HEADERS_INCOMPLETE"
E_GOAWAY_STREAM = "E_GOAWAY_STREAM"
E_SETTINGS_STATE = "E_SETTINGS_STATE"
E_PING_STATE = "E_PING_STATE"
E_FINAL_STATE = "E_FINAL_STATE"
E_SCENARIO_MISMATCH = "E_SCENARIO_MISMATCH"
E_RUN_NOT_FOUND = "E_RUN_NOT_FOUND"
E_RUN_EXISTS = "E_RUN_EXISTS"
E_DB_CORRUPT = "E_DB_CORRUPT"
E_TRAJECTORY_MISMATCH = "E_TRAJECTORY_MISMATCH"
E_INCOMPLETE_RUN = "E_INCOMPLETE_RUN"

