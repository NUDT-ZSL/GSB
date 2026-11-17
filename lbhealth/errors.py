"""Stable error codes used by CLI, reports, and the UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HealthError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class PoolValidationError(HealthError):
    errors: list[dict[str, str]] = field(default_factory=list)

    def __init__(self, errors: list[dict[str, str]]):
        super().__init__(
            "E_POOL",
            "backend pool configuration was rejected",
            {"errors": errors},
        )
        self.errors = errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "code": "E_POOL",
            "message": self.message,
            "errors": self.errors,
        }


@dataclass
class ConfigError(HealthError):
    pass


@dataclass
class LedgerError(HealthError):
    pass


@dataclass
class SnapshotError(HealthError):
    pass


@dataclass
class ReplayError(HealthError):
    pass
