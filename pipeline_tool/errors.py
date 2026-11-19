"""Business error codes and the PipelineError exception type."""

E_INPUT_MISSING = "E_INPUT_MISSING"
E_BAD_JSON = "E_BAD_JSON"
E_BAD_PIPELINE = "E_BAD_PIPELINE"
E_CYCLE = "E_CYCLE"
E_BAD_BUDGET = "E_BAD_BUDGET"
E_DIGEST_MISMATCH = "E_DIGEST_MISMATCH"
E_CLOCK_SKEW = "E_CLOCK_SKEW"
E_NODE_FAILED = "E_NODE_FAILED"
E_STATE_CONFLICT = "E_STATE_CONFLICT"


class PipelineError(Exception):
    """Business error carrying a stable code plus diagnosable fields."""

    def __init__(self, code: str, message: str, **fields):
        self.code = code
        self.message = message
        self.fields = fields
        super().__init__(f"{code}: {message} {fields if fields else ''}".strip())

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "fields": self.fields}
