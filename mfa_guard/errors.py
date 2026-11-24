"""Stable business error codes and structured issues."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class Codes:
    INPUT_JSON = "E_INPUT_JSON"
    INPUT_STRUCTURE = "E_INPUT_STRUCTURE"
    ID_INVALID = "E_ID_INVALID"
    DUP_USER = "E_DUP_USER"
    DUP_FACTOR = "E_DUP_FACTOR"
    FACTOR_TYPE = "E_FACTOR_TYPE"
    SCALE_RANGE = "E_SCALE_RANGE"
    DIGEST_FORMAT = "E_DIGEST_FORMAT"
    TOTP_FORMAT = "E_TOTP_FORMAT"
    COMPONENT_FORMAT = "E_COMPONENT_FORMAT"
    TIME_FORMAT = "E_TIME_FORMAT"
    RISK_FORMAT = "E_RISK_FORMAT"
    DUP_ATTEMPT = "E_DUP_ATTEMPT"
    POLICY_STRUCTURE = "E_POLICY_STRUCTURE"
    POLICY_CLOCK = "E_POLICY_CLOCK"
    POLICY_WINDOW = "E_POLICY_WINDOW"
    POLICY_GENERATION = "E_POLICY_GENERATION"
    POLICY_CONFLICT = "E_POLICY_CONFLICT"
    POLICY_KEY_CONFLICT = "E_POLICY_KEY_CONFLICT"
    POLICY_TOMBSTONE = "E_POLICY_TOMBSTONE"
    POLICY_DB = "E_POLICY_DB"
    POLICY_TYPE_DENIED = "E_POLICY_TYPE_DENIED"
    POLICY_UNKNOWN_FACTOR = "E_POLICY_UNKNOWN_FACTOR"
    UNKNOWN_USER = "E_UNKNOWN_USER"
    UNKNOWN_FACTOR = "E_UNKNOWN_FACTOR"
    FACTOR_NOT_BOUND = "E_FACTOR_NOT_BOUND"
    FACTOR_NOT_TRUSTED = "E_FACTOR_NOT_TRUSTED"
    DEVICE_MISMATCH = "E_DEVICE_MISMATCH"
    FP_AMBIGUOUS = "E_FP_AMBIGUOUS"
    FP_SHAPE = "E_FP_SHAPE"
    OTP_MISMATCH = "E_OTP_MISMATCH"
    OTP_AMBIGUOUS = "E_OTP_AMBIGUOUS"
    TICKET_REPLAY = "E_TICKET_REPLAY"
    TICKET_REVOKED = "E_TICKET_REVOKED"
    TICKET_CYCLE = "E_TICKET_CYCLE"
    TICKET_WINDOW_EXPIRED = "E_TICKET_WINDOW_EXPIRED"
    TICKET_GENERATION_EXCEEDED = "E_TICKET_GENERATION_EXCEEDED"
    TICKET_NOT_FOUND = "E_TICKET_NOT_FOUND"
    TIMEOUT = "E_TIMEOUT"
    QUEUE_FULL = "E_QUEUE_FULL"
    LOCK = "E_LOCK"
    CHECKPOINT_INVALID = "E_CHECKPOINT_INVALID"
    LEDGER_MISMATCH = "E_LEDGER_MISMATCH"
    ATOMIC_WRITE = "E_ATOMIC_WRITE"
    REPORT_INVALID = "E_REPORT_INVALID"


@dataclass(frozen=True)
class Issue:
    """A record-level rejection that does not stop processing."""

    source: str
    record_index: int
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "record_index": self.record_index,
            "code": self.code,
            "message": self.message,
            "detail": dict(self.detail),
        }


class MFAError(Exception):
    """Fatal operation error carrying a stable business code."""

    def __init__(
        self,
        code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


class LockConflict(MFAError):
    def __init__(self, path: str, message: str = "checkpoint lock is already held") -> None:
        super().__init__(Codes.LOCK, message, {"lock_path": str(path)})

