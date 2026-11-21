from __future__ import annotations

from typing import Any


class WalRecError(Exception):
    """A business error with a stable machine-readable code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "details": self.details,
            "message": self.message,
            "ok": False,
        }


def bad_wal(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_BAD_WAL", message, details=details)


def bad_checkpoint(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_BAD_CHECKPOINT", message, details=details)


def chain_break(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_CHAIN_BREAK", message, details=details)


def summary_mismatch(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_SUMMARY_MISMATCH", message, details=details)


def checkpoint_missing(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_CHECKPOINT_MISSING", message, details=details)


def replay_generation_error(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_REPLAY_GENERATION", message, details=details)


def clock_rollback(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_CLOCK_ROLLBACK", message, details=details)


def version_rollback(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_VERSION_ROLLBACK", message, details=details)


def policy_error(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_POLICY", message, details=details)


def state_error(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_STATE", message, details=details)


def apply_conflict(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_APPLY_CONFLICT", message, details=details)


def apply_failed(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_APPLY_FAILED", message, details=details)


def half_applied(message: str, **details: Any) -> WalRecError:
    return WalRecError("E_HALF_APPLIED", message, details=details)
