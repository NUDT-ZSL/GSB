"""Typed errors and stable error codes for the calibration system."""

from __future__ import annotations


class DriftAEError(RuntimeError):
    """Base class for all domain errors."""

    code = "E_DRIFT_AE"

    def __init__(self, message: str | None = None, *, details: dict | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details or {}

    def to_outcome(self) -> dict:
        return {
            "ok": False,
            "error_code": self.code,
            "error": self.message,
            "details": self.details,
        }


class StageBusyError(DriftAEError):
    code = "E_STAGE_BUSY"


class SnapshotRollbackError(DriftAEError):
    code = "E_SNAPSHOT_ROLLBACK"


class ChecksumMismatchError(DriftAEError):
    code = "E_CHECKSUM_MISMATCH"


class BudgetExceededError(DriftAEError):
    code = "E_BUDGET_EXCEEDED"


class ReplayMismatchError(DriftAEError):
    code = "E_REPLAY_MISMATCH"


class ThresholdFormatError(DriftAEError):
    code = "E_THRESHOLD_FORMAT"
