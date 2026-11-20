"""Stable business errors used by the CLI and all validation layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_CODES = {
    "E_JSON": "JSON is malformed or contains a non-finite number",
    "E_MANIFEST": "Manifest fields are missing or invalid",
    "E_SAMPLE_HASH": "A sample file digest does not match the manifest",
    "E_CLASS_SCHEMA": "A class definition has an invalid shape",
    "E_CLASS_DUPLICATE": "Class names must be unique",
    "E_CLASS_PARAM": "A class parameter is not a positive integer or is unsupported",
    "E_PEAK_BURST": "Peak burst depth must be at least the committed burst depth",
    "E_RATE_ORDER": "Peak rate must be at least the committed rate",
    "E_PARENT_UNKNOWN": "A parent class reference is not closed",
    "E_HIERARCHY_CYCLE": "The class hierarchy contains a cycle",
    "E_GUARANTEE_SUM": "Child guaranteed bandwidth exceeds the parent committed rate",
    "E_CLASSIFIER": "A classifier rule is invalid",
    "E_EVENT_SCHEMA": "An arrival event has an invalid shape",
    "E_EVENT_SEQ": "Event sequence numbers must be strictly increasing",
    "E_EVENT_TIME": "Event arrival time must be a non-negative integer",
    "E_EVENT_CLASS": "An event class label or classifier target is invalid",
    "E_EVENT_CLASS_MISMATCH": "An explicit event class disagrees with its classifier match",
    "E_EVENT_HORIZON": "An event falls outside the configured logical time horizon",
    "E_DB_NOT_LOADED": "The SQLite database has not been initialized",
    "E_DB_SCHEMA": "The SQLite database schema or metadata is invalid",
    "E_DB_SAMPLE_MISMATCH": "The requested sample differs from the loaded sample",
    "E_STALE_COMMIT": "The step changed while this instance was computing it",
    "E_CONFLICT": "A committed-state invariant was violated",
    "E_USAGE": "Command-line usage is invalid",
}


@dataclass
class QosError(Exception):
    """A deterministic, machine-readable business error."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown error code: {self.code}")
        super().__init__(f"{self.code}: {self.message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def fail(code: str, message: str, **details: Any) -> None:
    raise QosError(code=code, message=message, details=details)

