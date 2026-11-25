"""Typed application errors with stable machine-readable codes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


class EMemoryBudget(AppError):
    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__("E_MEMORY_BUDGET", message, details)


class EStageBusy(AppError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_STAGE_BUSY", message, details or {})


class ELedgerCorrupt(AppError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_LEDGER_CORRUPT", message, details or {})


class ETaskInvalid(AppError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_TASK_INVALID", message, details or {})


class EConfigInvalid(AppError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_CONFIG_INVALID", message, details or {})


class EForcedTermination(AppError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_FORCED_TERMINATION", message, details or {})
