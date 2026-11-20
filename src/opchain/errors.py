"""Business error codes, integer exit-code mapping and the unified exception."""


class ErrorCode:
    OK = "E_OK"
    OP_UNKNOWN = "E_OP_UNKNOWN"
    AXIS_INVALID = "E_AXIS_INVALID"
    PARAM_INVALID = "E_PARAM_INVALID"
    DTYPE_UNSUPPORTED = "E_DTYPE_UNSUPPORTED"
    TOLERANCE_EXCEEDED = "E_TOLERANCE_EXCEEDED"
    LATENCY_BUDGET = "E_LATENCY_BUDGET"
    INCONSISTENT = "E_INCONSISTENT"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"


EXIT_CODES = {
    ErrorCode.OK: 0,
    ErrorCode.OP_UNKNOWN: 10,
    ErrorCode.AXIS_INVALID: 11,
    ErrorCode.PARAM_INVALID: 12,
    ErrorCode.DTYPE_UNSUPPORTED: 13,
    ErrorCode.TOLERANCE_EXCEEDED: 14,
    ErrorCode.LATENCY_BUDGET: 15,
    ErrorCode.INCONSISTENT: 16,
    ErrorCode.RESOURCE_LIMIT: 17,
    ErrorCode.SNAPSHOT_CORRUPT: 18,
}


class OpChainError(Exception):
    """Carries a stable business code plus structured details for JSON output."""

    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
