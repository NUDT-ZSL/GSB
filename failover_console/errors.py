"""Typed business errors with machine-readable details."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .codes import BusinessCode


@dataclass(frozen=True)
class ValidationIssue:
    line: int
    field: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "field": self.field, "message": self.message}

    def __str__(self) -> str:
        return f"line {self.line} field {self.field}: {self.message}"


class BusinessError(Exception):
    """An expected domain error that is safe to expose to the operator."""

    def __init__(
        self,
        code: BusinessCode | str,
        message: str,
        *,
        details: Any | None = None,
        issues: list[ValidationIssue] | None = None,
    ) -> None:
        self.code = str(code)
        self.message = message
        self.details = details
        self.issues = issues or []
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        if self.issues:
            payload["issues"] = [issue.as_dict() for issue in self.issues]
        return payload


@dataclass
class ReplayDifference:
    kind: str
    field: str
    expected: Any
    actual: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class ConsistencyDeviation:
    service_id: str
    instance_id: str
    field: str
    expected: Any
    actual: Any

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "instance_id": self.instance_id,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
        }
