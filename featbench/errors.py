"""Business error codes and the single exception type used across the workbench."""

E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"
E_TARGET_MISSING = "E_TARGET_MISSING"
E_UNKNOWN_COLUMN = "E_UNKNOWN_COLUMN"
E_TYPE_MISMATCH = "E_TYPE_MISMATCH"
E_MISSING_UNFILLED = "E_MISSING_UNFILLED"
E_CONSTANT_COLUMN = "E_CONSTANT_COLUMN"
E_STAGE_FAILED = "E_STAGE_FAILED"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"


class BenchError(Exception):
    """Exception carrying a stable business code plus a human readable message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_failure(self, scale: str | None = None, stage: str | None = None,
                   exc_type: str | None = None) -> dict:
        return {
            "scale": scale,
            "stage": stage,
            "code": self.code,
            "message": self.message,
            "exception_type": exc_type or type(self).__name__,
        }
