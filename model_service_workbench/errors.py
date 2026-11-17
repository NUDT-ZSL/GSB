"""Business errors with stable codes and field paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class BusinessError(Exception):
    """A failure that callers can present without losing its business context."""

    code: str
    message: str
    path: str = "$"
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.code}: {self.message} ({self.path})")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "details": dict(self.details),
        }


class DeclarationError(BusinessError):
    def __init__(
        self,
        message: str,
        path: str = "$",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("E_DECL", message, path, details or {})


class DemandError(BusinessError):
    def __init__(
        self,
        message: str,
        path: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("E_DEMAND", message, path, details or {})


class AllocationError(BusinessError):
    def __init__(
        self,
        message: str,
        *,
        minute: int | None,
        service: str | None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        path = "$.minutes"
        if minute is not None:
            path += f"[{minute}]"
        if service is not None:
            path += f".services[{service}]"
        super().__init__(
            "E_ALLOC",
            message,
            path,
            {"minute": minute, "service": service, **dict(details or {})},
        )


class ReplayError(BusinessError):
    def __init__(
        self,
        message: str,
        *,
        minute: int | None = None,
        service: str | None = None,
        field: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        path = "$.runs"
        if minute is not None:
            path += f".minutes[{minute}]"
        if service is not None:
            path += f".services[{service}]"
        if field is not None:
            path += f".{field}"
        payload = {
            "minute": minute,
            "service": service,
            "field": field,
            **dict(details or {}),
        }
        super().__init__("E_REPLAY", message, path, payload)


class StateError(BusinessError):
    """A local state error that is not one of the four core replay codes."""

    def __init__(
        self,
        message: str,
        path: str = "$",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("E_STATE", message, path, details or {})
