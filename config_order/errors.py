"""Structured business errors shared by loader, engine, and CLI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


E_ENV = "E_ENV"
E_BIND = "E_BIND"
E_CHAIN = "E_CHAIN"
E_CYCLE = "E_CYCLE"
E_CONFLICT = "E_CONFLICT"
E_REPLAY = "E_REPLAY"
E_INTERNAL = "E_INTERNAL"


@dataclass(frozen=True)
class Issue:
    code: str
    field_path: str
    message: str
    environment_id: str | None = None
    config_key: str | None = None
    source_id: str | None = None
    rule_id: str | None = None
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "field_path": self.field_path,
            "message": self.message,
        }
        for name in (
            "environment_id",
            "config_key",
            "source_id",
            "rule_id",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.detail is not None:
            result["detail"] = self.detail
        return result


class LoadRejected(Exception):
    """An input failed validation and must be rejected as a whole."""

    def __init__(self, business_code: str, issues: list[Issue]):
        super().__init__(business_code)
        self.business_code = business_code
        self.issues = issues

    def as_report(self, diagnosis_parameters: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "status": "REJECTED",
            "business_code": self.business_code,
            "diagnosis_parameters": diagnosis_parameters,
            "errors": [issue.as_dict() for issue in self.issues],
        }


class ChainIntegrityError(Exception):
    """An expected chain digest did not match the computed chain."""

    def __init__(self, issues: list[Issue]):
        super().__init__(E_CHAIN)
        self.business_code = E_CHAIN
        self.issues = issues


class ReplayError(Exception):
    """A persisted diagnosis could not be reproduced."""

    def __init__(self, issues: list[Issue]):
        super().__init__(E_REPLAY)
        self.business_code = E_REPLAY
        self.issues = issues

