"""Business error codes shared by every pipeline stage."""
from __future__ import annotations

E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
E_PIXEL_CONFLICT = "E_PIXEL_CONFLICT"
E_INSTANCE_DUP = "E_INSTANCE_DUP"
E_LATENCY_BUDGET = "E_LATENCY_BUDGET"
E_INCONSISTENT = "E_INCONSISTENT"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"


class PanoError(Exception):
    """Structured business error.

    Attributes
    ----------
    code:    one of the E_* constants above.
    message: human readable description.
    details: machine readable context (field offset, position, stage, ...).
    """

    def __init__(self, code: str, message: str, **details):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}
