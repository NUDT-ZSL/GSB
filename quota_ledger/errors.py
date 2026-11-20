"""Business errors returned by the quota ledger."""

from __future__ import annotations

from typing import Any


E_BAD_HIERARCHY = "E_BAD_HIERARCHY"
E_BAD_QUOTA = "E_BAD_QUOTA"
E_BAD_EVENT = "E_BAD_EVENT"
E_BAD_LIMITER = "E_BAD_LIMITER"
E_NOT_LOADED = "E_NOT_LOADED"
E_CONFIG_CONFLICT = "E_CONFIG_CONFLICT"
E_CLOCK_ROLLBACK = "E_CLOCK_ROLLBACK"
E_IDEMPOTENCY_CONFLICT = "E_IDEMPOTENCY_CONFLICT"
E_EVENT_SEQ_CONFLICT = "E_EVENT_SEQ_CONFLICT"
E_OBJECT_NOT_FOUND = "E_OBJECT_NOT_FOUND"
E_RATE_LIMITED = "E_RATE_LIMITED"
E_HARD_QUOTA = "E_HARD_QUOTA"
E_INJECTED_FAILURE = "E_INJECTED_FAILURE"
E_RECOVERY_MISMATCH = "E_RECOVERY_MISMATCH"
E_LOCK_TIMEOUT = "E_LOCK_TIMEOUT"
E_DATABASE = "E_DATABASE"
E_INTERNAL = "E_INTERNAL"


class LedgerError(Exception):
    """A deterministic, serializable business error."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": self.code,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class InjectedFailure(RuntimeError):
    """Raised only by the explicit fault-injection test hook."""

    def __init__(self, stage: str) -> None:
        super().__init__(f"injected failure at {stage}")
        self.stage = stage

