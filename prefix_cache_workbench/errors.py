"""Business error codes and deterministic workbench exceptions."""

E_EMPTY_CONTEXT = "E_EMPTY_CONTEXT"
E_CONTEXT_TOO_LONG = "E_CONTEXT_TOO_LONG"
E_TOKEN_UNKNOWN = "E_TOKEN_UNKNOWN"
E_CHAIN_BROKEN = "E_CHAIN_BROKEN"
E_EVICT_POLICY_INVALID = "E_EVICT_POLICY_INVALID"
E_SNAPSHOT_STALE = "E_SNAPSHOT_STALE"
E_SESSION_LIMIT_EXCEEDED = "E_SESSION_LIMIT_EXCEEDED"

ALL_ERROR_CODES = (
    E_EMPTY_CONTEXT,
    E_CONTEXT_TOO_LONG,
    E_TOKEN_UNKNOWN,
    E_CHAIN_BROKEN,
    E_EVICT_POLICY_INVALID,
    E_SNAPSHOT_STALE,
    E_SESSION_LIMIT_EXCEEDED,
)

VALID_EVICTION_POLICIES = ("LRU", "LFU")


class WorkbenchError(Exception):
    """Exception carrying a stable business code and processing stage."""

    def __init__(self, code, stage, message=""):
        super().__init__(message or code)
        self.code = code
        self.stage = stage
        self.message = message or code

    def to_record(self, session, turn):
        return {
            "session": session,
            "turn": turn,
            "stage": self.stage,
            "code": self.code,
            "exception_type": type(self).__name__,
            "message": self.message,
        }
