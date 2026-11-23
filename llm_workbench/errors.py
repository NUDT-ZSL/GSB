"""Business error codes for the workbench.

Every failure path in the system is attributed to one of these codes and
carries a structured context (field, request_id, stage, ...) so the
diagnostics workbench can display where in the pipeline it happened.
"""

from __future__ import annotations


class ErrorCode:
    # batch / execution consistency
    E_BATCH_INCONSISTENT = "E_BATCH_INCONSISTENT"
    # admission budgets
    E_STEP_BUDGET = "E_STEP_BUDGET"
    E_KV_BUDGET = "E_KV_BUDGET"
    E_SLOT_BUDGET = "E_SLOT_BUDGET"
    # snapshots
    E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    # input / config validation
    E_DUP_REQUEST = "E_DUP_REQUEST"
    E_SAMPLING_INVALID = "E_SAMPLING_INVALID"
    E_LENGTH_INVALID = "E_LENGTH_INVALID"
    E_SEED_INVALID = "E_SEED_INVALID"
    E_VOCAB_MISSING = "E_VOCAB_MISSING"
    # cross-module self-check
    E_INCONSISTENT = "E_INCONSISTENT"


class WorkbenchError(Exception):
    """Structured business error.

    Attributes
    ----------
    code:    one of ErrorCode
    message: human readable summary
    context: dict with attribution fields, e.g.
             {"stage": "validate", "field": "top_p", "request_id": "r-003"}
    """

    def __init__(self, code: str, message: str, **context):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.context = context

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "context": self.context}
