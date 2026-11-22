"""Structured business errors used by the pipeline and CLI."""

from __future__ import annotations

from typing import Any, Optional


class BusinessError(Exception):
    """An attributable pipeline failure.

    The runner catches one error at a time, which keeps the error chain
    narrow and makes the field/clip/stage attribution explicit in reports.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: Optional[str] = None,
        clip_id: Optional[str] = None,
        stage: Optional[str] = None,
        version: Optional[int] = None,
        limit: Any = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.clip_id = clip_id
        self.stage = stage
        self.version = version
        self.limit = limit
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        for key, value in (
            ("field", self.field),
            ("clip_id", self.clip_id),
            ("stage", self.stage),
            ("version", self.version),
            ("limit", self.limit),
        ):
            if value is not None:
                result[key] = value
        if self.detail:
            result["detail"] = self.detail
        return result

    def __str__(self) -> str:
        location = ", ".join(
            f"{key}={value}"
            for key, value in (
                ("field", self.field),
                ("clip_id", self.clip_id),
                ("stage", self.stage),
                ("version", self.version),
            )
            if value is not None
        )
        suffix = f" ({location})" if location else ""
        return f"{self.code}: {self.message}{suffix}"
