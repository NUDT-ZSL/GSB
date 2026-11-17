"""Diagnostic error codes for the HTTP/2 multiplexing workbench."""

E_DECL = "E_DECL"      # declaration manifest validation failure
E_PROTO = "E_PROTO"    # connection/stream state machine violation
E_WINDOW = "E_WINDOW"  # flow-control ledger reconciliation failure
E_REPLAY = "E_REPLAY"  # snapshot replay is not reproducible


class DiagError(Exception):
    """Business diagnostic error carrying a stable code and field path."""

    def __init__(self, code, message, path=None, details=None):
        self.code = code
        self.message = message
        self.path = path
        self.details = details or {}
        where = f" [{path}]" if path else ""
        super().__init__(f"{code}: {message}{where}")

    def to_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "details": self.details,
        }
