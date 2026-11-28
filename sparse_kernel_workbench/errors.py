"""Business errors with stable codes and element-level context."""

from __future__ import annotations

from typing import Any


class WorkbenchError(Exception):
    """An input or execution error that is safe to expose as a business error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.context = context or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "error": {
                "code": self.code,
                "stage": self.stage,
                "message": self.message,
                "context": self.context,
            },
        }


def validation_error(
    code: str,
    message: str,
    *,
    stage: str = "normalization",
    version: str | None = None,
    file: str | None = None,
    line: int | None = None,
    element: str | None = None,
    field: str | None = None,
    raw_value: Any = None,
    reason: str | None = None,
    **extra: Any,
) -> WorkbenchError:
    context: dict[str, Any] = {}
    values = {
        "version": version,
        "file": file,
        "line": line,
        "element": element,
        "field": field,
        "raw_value": raw_value,
        "reason": reason,
    }
    context.update({key: value for key, value in values.items() if value is not None})
    context.update(extra)
    return WorkbenchError(code, message, stage=stage, context=context)
