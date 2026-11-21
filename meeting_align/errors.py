"""Business error codes raised by the alignment pipeline."""


class AlignError(Exception):
    """Base error carrying a stable business code."""

    code = "E_INTERNAL"

    def __init__(self, message="", **context):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.context = context

    def to_dict(self):
        return {"code": self.code, "message": self.message, "context": self.context}


class SampleRateMismatch(AlignError):
    code = "E_SAMPLE_RATE_MISMATCH"


class OverlapError(AlignError):
    code = "E_OVERLAP"


class OutOfRangeError(AlignError):
    code = "E_OUT_OF_RANGE"


class IntervalInvalidError(AlignError):
    code = "E_INTERVAL_INVALID"


class ResourceLimitError(AlignError):
    code = "E_RESOURCE_LIMIT"


class LineageBrokenError(AlignError):
    code = "E_LINEAGE_BROKEN"


class SnapshotRollbackError(AlignError):
    code = "E_SNAPSHOT_ROLLBACK"


class SnapshotCorruptError(AlignError):
    code = "E_SNAPSHOT_CORRUPT"
