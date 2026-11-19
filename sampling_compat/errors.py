"""Business error types with stable machine-readable codes."""

from __future__ import annotations

from typing import Any


class ErrorCode:
    VERSION_UNSUPPORTED = "E_VERSION_UNSUPPORTED"
    FORMAT_CORRUPT = "E_FORMAT_CORRUPT"
    PARAM_RANGE = "E_PARAM_RANGE"
    VOCAB_VERSION_MISMATCH = "E_VOCAB_VERSION_MISMATCH"
    TOKEN_RANGE = "E_TOKEN_RANGE"
    LOGITS_SHAPE = "E_LOGITS_SHAPE"
    SNAPSHOT_CORRUPT = "E_SNAPSHOT_CORRUPT"
    SEMANTIC_GAP = "E_SEMANTIC_GAP"
    INCONSISTENT = "E_INCONSISTENT"


class BusinessError(Exception):
    """Exception carrying enough context for the local UI and tests."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        field: str | None = None,
        step: int | None = None,
        entry: str | None = None,
        version: int | str | None = None,
        original_value: Any = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.request_id = request_id
        self.field = field
        self.step = step
        self.entry = entry
        self.version = version
        self.original_value = original_value
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
        optional = {
            "request_id": self.request_id,
            "field": self.field,
            "step": self.step,
            "entry": self.entry,
            "version": self.version,
            "original_value": self.original_value,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        return payload
