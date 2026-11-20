"""Structured business errors used across process and stage boundaries."""

from __future__ import annotations

from typing import Any


class EngineError(Exception):
    """A deterministic business error.

    ``code`` is the stable machine-readable business code.  The remaining
    attributes describe where the failure happened and which field or task is
    responsible.
    """

    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        field: str | None = None,
        method: str | None = None,
        batch_index: int | None = None,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.message = message
        self.field = field
        self.method = method
        self.batch_index = batch_index
        self.task_id = task_id
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "business_code": self.code,
            "stage": self.stage,
            "message": self.message,
        }
        optional = {
            "field": self.field,
            "method": self.method,
            "batch_index": self.batch_index,
            "task_id": self.task_id,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        if self.details:
            payload["details"] = self.details
        return payload


def coerce_engine_error(
    exc: BaseException,
    *,
    stage: str,
    code: str = "E_INTERNAL",
    method: str | None = None,
    batch_index: int | None = None,
    task_id: str | None = None,
) -> EngineError:
    if isinstance(exc, EngineError):
        if exc.method is None:
            exc.method = method
        if exc.batch_index is None:
            exc.batch_index = batch_index
        if exc.task_id is None:
            exc.task_id = task_id
        return exc
    return EngineError(
        code,
        stage,
        f"{type(exc).__name__}: {exc}",
        method=method,
        batch_index=batch_index,
        task_id=task_id,
        details={"exception_type": type(exc).__name__},
    )
