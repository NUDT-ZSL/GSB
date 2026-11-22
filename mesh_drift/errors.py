"""Business errors exposed by validation and reconciliation."""

from __future__ import annotations

from typing import Any

E_BAD_EXPECTED = "E_BAD_EXPECTED"
E_BAD_REPORT = "E_BAD_REPORT"
E_BAD_POLICY = "E_BAD_POLICY"
E_BAD_FIXTURE = "E_BAD_FIXTURE"
E_ROUND_CONFLICT = "E_ROUND_CONFLICT"
E_CLOCK_REGRESSION = "E_CLOCK_REGRESSION"

E_REPORT_CONNECTION = "E_REPORT_CONNECTION"
E_REPORT_TIMEOUT = "E_REPORT_TIMEOUT"
E_REPORT_MALFORMED = "E_REPORT_MALFORMED"
E_REPORT_HTTP = "E_REPORT_HTTP"
E_REPORT_TOO_LARGE = "E_REPORT_TOO_LARGE"
E_REPORT_IDENTITY = "E_REPORT_IDENTITY"
E_VERSION_REGRESSION = "E_VERSION_REGRESSION"
E_REPORT_CLOCK_REGRESSION = "E_REPORT_CLOCK_REGRESSION"
E_ROUND_BUDGET_EXHAUSTED = "E_ROUND_BUDGET_EXHAUSTED"


class DriftError(Exception):
    """A stable, machine-readable business error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }
        if self.path is not None:
            payload["path"] = self.path
        return payload

