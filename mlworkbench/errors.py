"""Business error codes for the diagnostic workbench."""

E_LABEL_UNKNOWN = "E_LABEL_UNKNOWN"
E_DECISION_INVALID = "E_DECISION_INVALID"
E_METRIC_MISMATCH = "E_METRIC_MISMATCH"
E_SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INCONSISTENT = "E_INCONSISTENT"


class BusinessError(Exception):
    """Error carrying a stable business code and structured details."""

    def __init__(self, code, message, **details):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}
