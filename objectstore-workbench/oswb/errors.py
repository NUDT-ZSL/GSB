"""Deterministic business error codes.

Every failure surfaced by the workbench maps to a stable business code so the
GUI and scripts can show a definite conclusion instead of a raw traceback.
"""

OK = "OK"
E_CHUNK = "E_CHUNK"
E_INTEGRITY = "E_INTEGRITY"
E_VERSION = "E_VERSION"
E_AUDIT = "E_AUDIT"
E_REPLAY = "E_REPLAY"
E_STATE = "E_STATE"
E_PARAM = "E_PARAM"
E_NOT_FOUND = "E_NOT_FOUND"
E_TRANSPORT = "E_TRANSPORT"


class BusinessError(Exception):
    """An exception carrying a stable business code plus structured detail."""

    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


CODE_SET = {
    OK,
    E_CHUNK,
    E_INTEGRITY,
    E_VERSION,
    E_AUDIT,
    E_REPLAY,
    E_STATE,
    E_PARAM,
    E_NOT_FOUND,
    E_TRANSPORT,
}
