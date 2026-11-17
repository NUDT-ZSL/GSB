from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


EXIT_CODES = {
    "E_PORT": 20,
    "E_POLICY": 21,
    "E_CYCLE": 22,
    "E_VERSION": 23,
    "E_REPLAY": 24,
    "E_NOT_FOUND": 25,
    "E_PROTOCOL": 26,
    "E_STATE": 27,
    "E_INTERNAL": 1,
}


@dataclass
class EdgeError(Exception):
    """A business error that can cross the JSON-line protocol boundary."""

    code: str
    message: str
    location: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, f"{self.code}: {self.message}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.location:
            payload["location"] = self.location
        if self.details:
            payload["details"] = self.details
        return payload

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.code, 1)


class PolicyError(EdgeError):
    def __init__(self, message: str, location: str | None = None, **details: Any) -> None:
        super().__init__("E_POLICY", message, location, details)


class CycleError(EdgeError):
    def __init__(self, message: str, location: str | None = None, **details: Any) -> None:
        super().__init__("E_CYCLE", message, location, details)


class PortError(EdgeError):
    def __init__(self, message: str, location: str | None = None, **details: Any) -> None:
        super().__init__("E_PORT", message, location, details)


class VersionError(EdgeError):
    def __init__(self, message: str, location: str | None = None, **details: Any) -> None:
        super().__init__("E_VERSION", message, location, details)


class ReplayError(EdgeError):
    def __init__(self, message: str, location: str | None = None, **details: Any) -> None:
        super().__init__("E_REPLAY", message, location, details)


class ProtocolError(EdgeError):
    def __init__(self, message: str, location: str | None = None, **details: Any) -> None:
        super().__init__("E_PROTOCOL", message, location, details)


class StateError(EdgeError):
    def __init__(self, message: str, location: str | None = None, **details: Any) -> None:
        super().__init__("E_STATE", message, location, details)


def error_payload(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, EdgeError):
        return exc.to_dict()
    return {
        "code": "E_INTERNAL",
        "message": str(exc) or exc.__class__.__name__,
        "details": {"exception": exc.__class__.__name__},
    }


def exit_code_for(exc: BaseException) -> int:
    if isinstance(exc, EdgeError):
        return exc.exit_code
    return EXIT_CODES["E_INTERNAL"]

