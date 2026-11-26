"""Domain errors with stable machine-readable codes."""

from __future__ import annotations


class DriftError(Exception):
    """An expected failure that callers can replay by error code."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


E_VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_CHECKPOINT_CORRUPT = "E_CHECKPOINT_CORRUPT"
E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_ALIGNMENT_INVALID = "E_ALIGNMENT_INVALID"
E_SNAPSHOT_INVALID = "E_SNAPSHOT_INVALID"
E_CACHE_CORRUPT = "E_CACHE_CORRUPT"
E_LEDGER_REPLAY = "E_LEDGER_REPLAY"
E_IO = "E_IO"

