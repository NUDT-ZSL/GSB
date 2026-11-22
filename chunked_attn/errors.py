"""Structured business error codes for the chunked attention component."""
from __future__ import annotations

# Validation chain (requirement 7)
E_DUPLICATE_CASE_ID = "E_DUPLICATE_CASE_ID"
E_DIM_MISMATCH = "E_DIM_MISMATCH"
E_INVALID_BLOCK = "E_INVALID_BLOCK"
E_HEADS_MISMATCH = "E_HEADS_MISMATCH"
E_INVALID_CAUSAL = "E_INVALID_CAUSAL"
E_INVALID_DTYPE = "E_INVALID_DTYPE"
E_MISSING_FIELD = "E_MISSING_FIELD"

# Runtime business codes
E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
E_BLOCK_MISMATCH = "E_BLOCK_MISMATCH"
E_STABILITY_VIOLATION = "E_STABILITY_VIOLATION"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_INCONSISTENT = "E_INCONSISTENT"


class BizError(Exception):
    """Business error carrying a stable machine-readable code.

    code:    one of the E_* constants above.
    field:   manifest/config field the error is attributed to (if any).
    case_id: sample the error is attributed to (if any).
    stage:   pipeline stage name, e.g. "manifest_validate", "array_validate",
             "plan", "compute", "consistency", "stability", "snapshot",
             "selfcheck". Sample-level failures are always attributed to a
             concrete stage and never silently dropped.
    details: free-form structured payload (e.g. completed block list).
    """

    def __init__(self, code, message, *, field=None, case_id=None, stage=None,
                 details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.case_id = case_id
        self.stage = stage
        self.details = details or {}

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "case_id": self.case_id,
            "stage": self.stage,
            "details": self.details,
        }
