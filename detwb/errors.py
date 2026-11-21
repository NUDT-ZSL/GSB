"""业务错误码与统一异常类型。"""


class Err:
    FORMAT_VERSION_UNSUPPORTED = "E_FORMAT_VERSION_UNSUPPORTED"
    FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
    SEMANTIC_DRIFT = "E_SEMANTIC_DRIFT"
    SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    LABEL_RETIRED = "E_LABEL_RETIRED"
    CACHE_CORRUPT = "E_CACHE_CORRUPT"
    STAGE_BUSY = "E_STAGE_BUSY"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    INCONSISTENT = "E_INCONSISTENT"


class WorkbenchError(Exception):
    """携带业务错误码与归因信息（行号/字段/类别）的异常。"""

    def __init__(self, code, message, *, line=None, field=None, category=None, details=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.line = line
        self.field = field
        self.category = category
        self.details = details or {}

    def to_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.line is not None:
            out["line"] = self.line
        if self.field is not None:
            out["field"] = self.field
        if self.category is not None:
            out["category"] = self.category
        if self.details:
            out["details"] = self.details
        return out
