from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ERROR_CODES = {
    "E_INPUT_INVALID": "Input failed validation",
    "E_DUPLICATE_SERVICE": "Service name is duplicated",
    "E_DUPLICATE_NODE": "Node name is duplicated",
    "E_DUPLICATE_WINDOW_TIME": "Workload window time is duplicated",
    "E_SERVICE_UNKNOWN": "Service is not declared",
    "E_CLOCK_REGRESSION": "Logical clock regression exceeds tolerance",
    "E_NO_OBSERVATION": "No workload observation exists at the logical clock",
    "E_CONFIG_VERSION_REGRESSION": "Configuration version would move backwards",
    "E_CONFIG_VERSION_CONFLICT": "Configuration version was reused with different content",
    "E_STATE_VERSION_CONFLICT": "Snapshot compare-and-swap failed",
    "E_LEDGER_MISMATCH": "GPU or concurrency ledger is not conserved",
    "E_LAUNCH_CONNECTION_REFUSED": "Launch target refused the connection",
    "E_COLD_START_BUDGET_EXCEEDED": "Cold start exceeded its logical budget",
    "E_LAUNCH_HTTP_ERROR": "Launch target returned an HTTP error",
    "E_LAUNCH_RESPONSE_INVALID": "Launch target response is invalid",
    "E_STUB_UNAVAILABLE": "The local launch stub could not be reached",
    "E_REQUEST_IN_PROGRESS": "The idempotent request is already in progress",
    "E_REQUEST_CONFLICT": "The idempotency key was reused with a different request",
    "E_STORAGE_ERROR": "Persistent state operation failed",
    "E_UNSAFE_SPEC_CHANGE": "The specification cannot be changed while replicas are active",
    "E_FORCED_TERMINATION": "The process was intentionally terminated for the recovery drill",
}


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def input_error(message: str, **details: Any) -> BusinessError:
    return BusinessError("E_INPUT_INVALID", message, details)
