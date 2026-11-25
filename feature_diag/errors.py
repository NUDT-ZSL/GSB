from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_PARAM_INVALID = "E_PARAM_INVALID"
E_BAD_INPUT = "E_BAD_INPUT"
E_ENCODING_ERROR = "E_ENCODING_ERROR"
E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_STALE_RESULT = "E_STALE_RESULT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
E_INTERNAL_ERROR = "E_INTERNAL_ERROR"


@dataclass
class EngineError(Exception):
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    completed_steps: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
            "completed_steps": list(self.completed_steps),
        }


def param_error(message: str, **detail: Any) -> EngineError:
    return EngineError(E_PARAM_INVALID, message, detail)


def bad_input(message: str, **detail: Any) -> EngineError:
    return EngineError(E_BAD_INPUT, message, detail)


def encoding_error(message: str, **detail: Any) -> EngineError:
    return EngineError(E_ENCODING_ERROR, message, detail)


def snapshot_error(message: str, **detail: Any) -> EngineError:
    return EngineError(E_SNAPSHOT_ROLLBACK, message, detail)


def resource_error(message: str, **detail: Any) -> EngineError:
    return EngineError(E_RESOURCE_LIMIT, message, detail)


def stage_busy(message: str = "another writer holds the cache commit stage", **detail: Any) -> EngineError:
    return EngineError(E_STAGE_BUSY, message, detail)


def stale_result(message: str, **detail: Any) -> EngineError:
    return EngineError(E_STALE_RESULT, message, detail)
