"""Business error codes for the MT data-preparation pipeline."""


class Codes:
    DUP_PAIR = "E_DUP_PAIR"
    EMPTY_SENT = "E_EMPTY_SENT"
    ALIGN_RANGE = "E_ALIGN_RANGE"
    VOCAB_TOKEN = "E_VOCAB_TOKEN"
    LEN_MISMATCH = "E_LEN_MISMATCH"
    SCHEMA = "E_SCHEMA"
    VOCAB_EMPTY = "E_VOCAB_EMPTY"
    CONFIG_INVALID = "E_CONFIG_INVALID"
    THRESHOLD_INVALID = "E_THRESHOLD_INVALID"
    PATH_INCOMPLETE = "E_PATH_INCOMPLETE"
    PATH_DIVERGENCE = "E_PATH_DIVERGENCE"
    COMPARE_BUDGET = "E_COMPARE_BUDGET"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
    SNAPSHOT_ROLLBACK = "E_SNAPSHOT_ROLLBACK"
    SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    INCONSISTENT = "E_INCONSISTENT"


class PipelineError(Exception):
    """Structured pipeline error carrying a business code and location info."""

    def __init__(self, code, message, pair_id=None, position=None, detail=None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.pair_id = pair_id
        self.position = position
        self.detail = detail

    def to_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.pair_id is not None:
            out["pair_id"] = self.pair_id
        if self.position is not None:
            out["position"] = self.position
        if self.detail is not None:
            out["detail"] = self.detail
        return out
