"""Structured business error codes for the quantized conv pipeline."""
from __future__ import annotations

import json


class Code:
    # validation chain
    E_DUPLICATE_JOB_ID = "E_DUPLICATE_JOB_ID"
    E_SHAPE_MISMATCH = "E_SHAPE_MISMATCH"
    E_INVALID_QUANT_SCALE = "E_INVALID_QUANT_SCALE"
    E_INVALID_ZERO_POINT = "E_INVALID_ZERO_POINT"
    E_UNSUPPORTED_ACC_DTYPE = "E_UNSUPPORTED_ACC_DTYPE"
    E_REVISION_ROLLBACK = "E_REVISION_ROLLBACK"
    E_BAD_MANIFEST = "E_BAD_MANIFEST"
    # numeric / tolerance
    E_TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
    # lineage / staleness
    E_STALE_RESULT = "E_STALE_RESULT"
    E_LINEAGE_BROKEN = "E_LINEAGE_BROKEN"
    # staging / resources
    E_STAGE_BUSY = "E_STAGE_BUSY"
    E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    # consistency self-check
    E_INCONSISTENT = "E_INCONSISTENT"


class PipelineError(Exception):
    """A structured business error.

    Attributes:
        code: one of :class:`Code` constants.
        job_id: offending job, when applicable.
        field: offending manifest field, when applicable.
        node: DAG node the failure is attributed to, when applicable.
        details: free-form structured payload (version diffs, positions...).
    """

    def __init__(self, code, message, job_id=None, field=None, node=None, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.job_id = job_id
        self.field = field
        self.node = node
        self.details = details

    def to_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.job_id is not None:
            out["job_id"] = self.job_id
        if self.field is not None:
            out["field"] = self.field
        if self.node is not None:
            out["node"] = self.node
        if self.details:
            out["details"] = self.details
        return out

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def error_dict(code, message, job_id=None, field=None, node=None, **details):
    """Build a structured error dict without raising (used in reports)."""
    return PipelineError(code, message, job_id=job_id, field=field,
                         node=node, **details).to_dict()
