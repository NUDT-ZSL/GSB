"""Business error codes and the unified exception type."""

E_DUP_DOC = "E_DUP_DOC"
E_TERM_INVALID = "E_TERM_INVALID"
E_BRIDGE_LANG = "E_BRIDGE_LANG"
E_CONFIDENCE_RANGE = "E_CONFIDENCE_RANGE"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_BUILD_BUSY = "E_BUILD_BUSY"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_INCONSISTENT = "E_INCONSISTENT"


class XError(Exception):
    """Business error carrying a stable code and attribution context."""

    def __init__(self, code, message, **context):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.context = {k: v for k, v in context.items() if v is not None}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "context": self.context}
