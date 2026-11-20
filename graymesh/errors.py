"""Business error codes. Every failure path maps to exactly one code."""


class ErrorCode:
    # rule / patch errors (control plane)
    REF_MISSING = "E_REF_MISSING"
    CYCLE = "E_CYCLE"
    FIELD_INVALID = "E_FIELD_INVALID"
    PATCH_FORMAT = "E_PATCH_FORMAT"
    VERSION_MISMATCH = "E_VERSION_MISMATCH"
    # upstream errors (data plane)
    UPSTREAM_CONN_REFUSED = "E_UPSTREAM_CONN_REFUSED"
    UPSTREAM_TIMEOUT = "E_UPSTREAM_TIMEOUT"
    UPSTREAM_MALFORMED = "E_UPSTREAM_MALFORMED"
    UPSTREAM_DISCONNECT = "E_UPSTREAM_DISCONNECT"


class BusinessError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
