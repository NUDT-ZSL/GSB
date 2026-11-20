"""Typed business error carrying a code from codes.py."""

from __future__ import annotations

from typing import Any, Optional


class BusinessError(Exception):
    """An expected, attributable failure in one of the engine stages."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        chart_id: Optional[str] = None,
        stage: Optional[str] = None,
        field: Optional[str] = None,
        series_id: Optional[str] = None,
        detail: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.chart_id = chart_id
        self.stage = stage
        self.field = field
        self.series_id = series_id
        self.detail = detail

    def to_record(self) -> dict:
        rec = {"code": self.code, "message": self.message}
        if self.stage is not None:
            rec["stage"] = self.stage
        if self.chart_id is not None:
            rec["chart_id"] = self.chart_id
        if self.field is not None:
            rec["field"] = self.field
        if self.series_id is not None:
            rec["series_id"] = self.series_id
        if self.detail is not None:
            rec["detail"] = self.detail
        return rec
