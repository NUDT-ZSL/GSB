from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DiagnosticIssue:
    stage: str
    code: str
    message: str
    severity: str = "warning"
    action: str = "degrade"
    degrade_category: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "action": self.action,
            "degrade_category": self.degrade_category,
            "details": self.details,
        }


class DiagnosticError(RuntimeError):
    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        *,
        degrade_category: str | None = None,
        details: dict[str, Any] | None = None,
        first_divergence: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code} [{stage}]: {message}")
        self.code = code
        self.stage = stage
        self.message = message
        self.degrade_category = degrade_category
        self.details = details or {}
        self.first_divergence = first_divergence

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "degrade_category": self.degrade_category,
            "details": self.details,
            "first_divergence": self.first_divergence,
        }

