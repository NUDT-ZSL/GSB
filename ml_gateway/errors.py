from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class GatewayError(Exception):
    """A stable, machine-readable business error."""

    code: str
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.details:
            payload["details"] = self.details
        return payload


class RuleError(GatewayError):
    def __init__(self, message: str, path: str, **details: Any) -> None:
        super().__init__("E_RULE", message, path, details)


class VectorError(GatewayError):
    def __init__(self, message: str, path: str, **details: Any) -> None:
        super().__init__("E_VECTOR", message, path, details)


class SnapshotError(GatewayError):
    def __init__(self, message: str, path: str | None = None, **details: Any) -> None:
        super().__init__("E_SNAP", message, path, details)


class ReplayError(GatewayError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("E_REPLAY", message, None, details)


class UpstreamError(GatewayError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("E_UPSTREAM", message, None, details)


class InputError(GatewayError):
    def __init__(self, message: str, path: str, **details: Any) -> None:
        super().__init__("E_INPUT", message, path, details)


class RouteError(GatewayError):
    def __init__(self, message: str, path: str, **details: Any) -> None:
        super().__init__("E_ROUTE", message, path, details)


class PortError(GatewayError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("E_PORT", message, None, details)


class StateError(GatewayError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("E_STATE", message, None, details)
