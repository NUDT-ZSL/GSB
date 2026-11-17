"""Business error codes and structured failures."""

E_POLICY_UNKNOWN_KEY = "E_POLICY_UNKNOWN_KEY"
E_POLICY_TYPE = "E_POLICY_TYPE"
E_POLICY_RANGE = "E_POLICY_RANGE"
E_SEED_WIDTH = "E_SEED_WIDTH"
E_SCORE_EMPTY = "E_SCORE_EMPTY"
E_SCORE_MISSING = "E_SCORE_MISSING"
E_SNAPSHOT_STALE = "E_SNAPSHOT_STALE"
E_REQUEST_MISSING = "E_REQUEST_MISSING"


class WorkbenchError(Exception):
    """Error carrying a business code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self):
        return {"code": self.code, "message": self.message}


def failure(request_id, stage, code, message, injection_id=None):
    rec = {
        "request_id": request_id,
        "stage": stage,
        "code": code,
        "message": message,
    }
    if injection_id is not None:
        rec["injection_id"] = injection_id
    return rec
