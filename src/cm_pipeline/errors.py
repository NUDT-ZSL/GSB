from __future__ import annotations

from typing import Any


class PipelineError(Exception):
    """A stable, machine-readable business error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str | None = None,
        field: str | None = None,
        pair_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.field = field
        self.pair_id = pair_id
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "field": self.field,
            "pair_id": self.pair_id,
        }
        if self.details:
            result["details"] = self.details
        return result

    def at_stage(self, stage: str) -> "PipelineError":
        if self.stage is None:
            self.stage = stage
        return self

