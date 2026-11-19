"""Business errors with stable machine-readable codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class WorkbenchError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


def config_error(field: str, message: str, value: Any = None) -> WorkbenchError:
    details: dict[str, Any] = {"field": field}
    if value is not None:
        details["value"] = value
    return WorkbenchError("E_CONFIG_INVALID", message, details)


def snapshot_corrupt(stage: str, version: int, message: str) -> WorkbenchError:
    return WorkbenchError(
        "E_SNAPSHOT_CORRUPT",
        message,
        {"stage": stage, "version": version},
    )


def snapshot_rollback(stage: str, incoming: int, current: int) -> WorkbenchError:
    return WorkbenchError(
        "E_SNAPSHOT_ROLLBACK",
        f"snapshot stage {stage!r} cannot move from version {current} to {incoming}",
        {"stage": stage, "incoming_version": incoming, "current_version": current},
    )


def tolerance_exceeded(
    *,
    precision: str,
    round_number: int,
    location: str,
    observed: float,
    tolerance: float,
    parameter: str | None = None,
) -> WorkbenchError:
    details: dict[str, Any] = {
        "precision": precision,
        "round": round_number,
        "location": location,
        "observed": observed,
        "tolerance": tolerance,
    }
    if parameter is not None:
        details["parameter"] = parameter
    return WorkbenchError(
        "E_TOLERANCE_EXCEEDED",
        "chunked training exceeds the configured consistency tolerance",
        details,
    )


def inconsistent(category: str, message: str, **details: Any) -> WorkbenchError:
    payload = {"category": category}
    payload.update(details)
    return WorkbenchError("E_INCONSISTENT", message, payload)
