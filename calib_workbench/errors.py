"""Structured business error codes for the calibration workbench.

Every failure surfaced to callers carries a stable machine-readable code,
the pipeline stage that raised it, and optional field/details for
attribution.  Workers in the process pool serialize these back to the
main process via ``to_dict`` / ``from_dict``.
"""

from __future__ import annotations


class WorkbenchError(Exception):
    """Business error with a stable code, stage name and attribution info."""

    def __init__(self, code, stage, message, field=None, details=None):
        super().__init__(f"[{code}] stage={stage} field={field}: {message}")
        self.code = code
        self.stage = stage
        self.message = message
        self.field = field
        self.details = dict(details or {})

    def to_dict(self):
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "field": self.field,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, payload):
        return cls(
            payload["code"],
            payload["stage"],
            payload["message"],
            field=payload.get("field"),
            details=payload.get("details"),
        )


# --- input / config validation (requirement 8) ---
E_DUP_SAMPLE_ID = "E_DUP_SAMPLE_ID"
E_FEATURE_LENGTH = "E_FEATURE_LENGTH"
E_GROUP_MISSING = "E_GROUP_MISSING"
E_UNKNOWN_CALIBRATOR = "E_UNKNOWN_CALIBRATOR"
E_INVALID_BINS = "E_INVALID_BINS"
E_INVALID_SPLIT_RATIO = "E_INVALID_SPLIT_RATIO"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_MANIFEST_INVALID = "E_MANIFEST_INVALID"

# --- resource limits (requirement 8) ---
E_BATCH_LIMIT = "E_BATCH_LIMIT"
E_MEM_TABLE_LIMIT = "E_MEM_TABLE_LIMIT"
E_PARALLEL_LIMIT = "E_PARALLEL_LIMIT"

# --- budget admission (requirement 5) ---
E_ITER_BUDGET = "E_ITER_BUDGET"
E_MEMORY_BUDGET = "E_MEMORY_BUDGET"

# --- snapshot store (requirement 6) ---
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"

# --- lineage / self-check (requirements 7, 8) ---
E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
E_INCONSISTENT = "E_INCONSISTENT"

# --- worker execution (requirement 4) ---
E_WORKER_FAILURE = "E_WORKER_FAILURE"
