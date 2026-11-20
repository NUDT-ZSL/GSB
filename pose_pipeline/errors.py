from __future__ import annotations

from typing import Any


class PipelineError(Exception):
    def __init__(
        self,
        code: str,
        field: str,
        message: str,
        *,
        image_id: str | None = None,
        category: str | None = None,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field = field
        self.message = message
        self.image_id = image_id
        self.category = category
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "severity": "error",
            "field": self.field,
            "message": self.message,
        }
        if self.image_id is not None:
            result["image_id"] = self.image_id
        if self.category is not None:
            result["category"] = self.category
        if self.details is not None:
            result["details"] = self.details
        return result
