"""Business error codes shared by every module of the engine.

All recoverable, domain-level failures raise :class:`EngineError` carrying a
stable error code string so that reports and the diagnostic UI can present the
same vocabulary.  Pure programming mistakes are still allowed to raise the
built-in exception types.
"""

from __future__ import annotations


E_CONFIG_INVALID = "E_CONFIG_INVALID"
E_DUPLICATE_DIALOG = "E_DUPLICATE_DIALOG"
E_TRUTH_MISMATCH = "E_TRUTH_MISMATCH"
E_RESOURCE_LIMIT = "E_RESOURCE_LIMIT"
E_STAGE_BUSY = "E_STAGE_BUSY"
E_SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
E_INCONSISTENT = "E_INCONSISTENT"
E_INPUT_INVALID = "E_INPUT_INVALID"


class EngineError(Exception):
    """Domain error with a stable ``code`` and an explicit ``field``/``item``."""

    def __init__(self, code: str, message: str, *, field: str | None = None,
                 item: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.item = item

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
            "item": self.item,
        }

    def __str__(self) -> str:  # pragma: no cover - trivial
        loc = self.field or self.item
        return f"[{self.code}] {self.message}" + (f" (at {loc})" if loc else "")
