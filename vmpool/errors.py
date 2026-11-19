"""Business error types shared by the parser, engine, store and CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


E_SAMPLE_MISSING = "E_SAMPLE_MISSING"
E_JSON_MALFORMED = "E_JSON_MALFORMED"
E_NAME_DUPLICATE = "E_NAME_DUPLICATE"
E_DEPENDENCY_MISSING = "E_DEPENDENCY_MISSING"
E_DEPENDENCY_CYCLE = "E_DEPENDENCY_CYCLE"
E_INVALID_NUMBER = "E_INVALID_NUMBER"
E_INVALID_REQUEST = "E_INVALID_REQUEST"
E_REQUEST_ORDER = "E_REQUEST_ORDER"
E_PENDING_CONFLICT = "E_PENDING_CONFLICT"
E_WITHDRAW_INVALID = "E_WITHDRAW_INVALID"
E_WITHDRAW_NOT_ACCEPTED = "E_WITHDRAW_NOT_ACCEPTED"
E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
E_DIGEST_MISMATCH = "E_DIGEST_MISMATCH"
E_INITIAL_STATE = "E_INITIAL_STATE"
E_POOL_QUOTA = "E_POOL_QUOTA"
E_BUDGET = "E_BUDGET"
E_SAFETY_MARGIN = "E_SAFETY_MARGIN"
E_DEPENDENCY_CAPACITY = "E_DEPENDENCY_CAPACITY"
E_NOT_LOADED = "E_NOT_LOADED"
E_SCENARIO_CONFLICT = "E_SCENARIO_CONFLICT"
E_REQUEST_CONFLICT = "E_REQUEST_CONFLICT"
E_VERSION_CONFLICT = "E_VERSION_CONFLICT"
E_DB_BUSY = "E_DB_BUSY"
E_DB_INTEGRITY = "E_DB_INTEGRITY"
E_USAGE = "E_USAGE"


@dataclass
class BusinessError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "message": self.message,
            "details": self.details,
        }


class IntegrityError(BusinessError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            E_DB_INTEGRITY, message, details or {}, exit_code=4
        )
