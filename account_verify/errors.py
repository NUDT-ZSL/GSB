from __future__ import annotations

from typing import Any, Dict, Optional


class VerificationError(Exception):
    """Base error with a stable machine-readable code."""

    code = "E_VERIFICATION"

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class LockError(VerificationError):
    code = "E_LOCK"


class CheckpointError(VerificationError):
    code = "E_CHECKPOINT_CORRUPT"


class LedgerIntegrityError(VerificationError):
    code = "E_LEDGER_DIGEST_MISMATCH"


class CrashInjected(RuntimeError):
    """Deterministic fault used by recovery tests."""
