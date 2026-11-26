"""Structured business errors used by the CLI and library API."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any


E_FORMAT = "E_FORMAT"
E_UNSUPPORTED = "E_UNSUPPORTED"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_STALE_RESULT = "E_STALE_RESULT"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_CONFIG = "E_CONFIG"


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    task_id: str | None = None
    field: str | None = None
    details: dict[str, Any] = dataclass_field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "task_id": self.task_id,
            "field": self.field,
            "details": self.details,
        }


def format_error(
    message: str,
    *,
    task_id: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(E_FORMAT, message, task_id, field, details or {})


def unsupported(
    message: str,
    *,
    task_id: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(E_UNSUPPORTED, message, task_id, field, details or {})


def corrupt(
    message: str,
    *,
    task_id: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(E_SNAPSHOT_CORRUPT, message, task_id, field, details or {})


def semantic_drift(
    message: str,
    *,
    task_id: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(E_SEMANTIC_DRIFT, message, task_id, field, details or {})


def stage_busy(message: str, *, details: dict[str, Any] | None = None) -> BusinessError:
    return BusinessError(E_STAGE_BUSY, message, None, "revision", details or {})


def stale_result(
    message: str,
    *,
    task_id: str,
    field: str,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(E_STALE_RESULT, message, task_id, field, details or {})


def config_error(
    message: str,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(E_CONFIG, message, None, field, details or {})


def resource_limit(
    message: str,
    *,
    task_id: str | None = None,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> BusinessError:
    return BusinessError(E_RESOURCE_LIMIT, message, task_id, field, details or {})

