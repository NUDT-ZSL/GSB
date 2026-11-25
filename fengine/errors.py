"""Business error codes and the BizError exception.

Every validation / runtime rejection carries a stable machine-readable code
plus a details dict that preserves diagnosable fields.
"""


class BizError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


# graph validation
E_GRAPH_DUPLICATE_NODE = "E_GRAPH_DUPLICATE_NODE"
E_GRAPH_UNKNOWN_DEP = "E_GRAPH_UNKNOWN_DEP"
E_GRAPH_CYCLE = "E_GRAPH_CYCLE"
E_GRAPH_EMPTY = "E_GRAPH_EMPTY"
# operator validation
E_OP_TYPE_INVALID = "E_OP_TYPE_INVALID"
E_OP_PARAM_MISMATCH = "E_OP_PARAM_MISMATCH"
E_OP_OUTPUT_TYPE_MISMATCH = "E_OP_OUTPUT_TYPE_MISMATCH"
E_OP_UNKNOWN_COLUMN = "E_OP_UNKNOWN_COLUMN"
# config validation
E_CONFIG_INVALID = "E_CONFIG_INVALID"
# event validation
E_EVENT_SEQ_ORDER = "E_EVENT_SEQ_ORDER"
E_EVENT_DIGEST_MISMATCH = "E_EVENT_DIGEST_MISMATCH"
E_EVENT_FIELD_INVALID = "E_EVENT_FIELD_INVALID"
# runtime / scheduling
E_MEMORY_BUDGET_EXHAUSTED = "E_MEMORY_BUDGET_EXHAUSTED"
E_NOT_LOADED = "E_NOT_LOADED"
# versioning
E_VERSION_ROLLBACK = "E_VERSION_ROLLBACK"
E_VERSION_INVALID_PATCH = "E_VERSION_INVALID_PATCH"
