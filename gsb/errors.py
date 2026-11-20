from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GSBError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 2

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
            },
        }
        if self.details:
            payload["error"]["details"] = self.details
        return payload


class GSBUsageError(GSBError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_USAGE", message, details or {}, 2)


class GSBBadBatchError(GSBError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_BAD_BATCH", message, details or {}, 3)


class GSBBadQueryError(GSBError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_BAD_QUERY", message, details or {}, 3)


class GSBBadConfigError(GSBError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_BAD_CONFIG", message, details or {}, 3)


class GSBStateConflictError(GSBError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_STATE_CONFLICT", message, details or {}, 4)


class GSBBatchConflictError(GSBError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_BATCH_CONFLICT", message, details or {}, 4)


class GSBMemoryBudgetError(GSBError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_MEMORY_BUDGET", message, details or {}, 5)


class GSBCorruptStateError(GSBError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_CORRUPT_STATE", message, details or {}, 6)


class GSBVerifyError(GSBError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_VERIFY_FAILED", message, details or {}, 7)


class GSBIoError(GSBError):
    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__("E_IO", message, details or {}, 8)

