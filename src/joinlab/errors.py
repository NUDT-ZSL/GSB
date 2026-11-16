"""Structured business errors used by every JoinLab entry point."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


ERROR_CODES = {
    "E_SCHEMA",
    "E_QUERY",
    "E_TYPE",
    "E_PLAN",
    "E_SKEW",
    "E_SNAP",
    "E_STALE",
    "E_CLI",
    "E_GUI",
}


@dataclass
class JoinLabError(Exception):
    """An expected, machine-readable application failure.

    ``path`` is a concrete JSON pointer-like location (for example
    ``tables[1].columns[3].type``) whenever the failure originates in an input
    document.  Keeping it structured makes atomic validation failures useful
    to both the CLI and the GUI.
    """

    code: str
    message: str
    path: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown business error code: {self.code}")
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "path": self.path,
            "details": self.details,
        }


class SchemaError(JoinLabError):
    def __init__(self, message: str, path: str = "", **details: Any) -> None:
        super().__init__("E_SCHEMA", message, path, details)


class QueryError(JoinLabError):
    def __init__(self, message: str, path: str = "", **details: Any) -> None:
        super().__init__("E_QUERY", message, path, details)


class TypeMismatchError(JoinLabError):
    def __init__(self, message: str, path: str = "", **details: Any) -> None:
        super().__init__("E_TYPE", message, path, details)


class PlanError(JoinLabError):
    def __init__(self, message: str, path: str = "", **details: Any) -> None:
        super().__init__("E_PLAN", message, path, details)


class SkewError(JoinLabError):
    def __init__(self, message: str, path: str = "", **details: Any) -> None:
        super().__init__("E_SKEW", message, path, details)


class SnapshotError(JoinLabError):
    def __init__(self, message: str, path: str = "", **details: Any) -> None:
        super().__init__("E_SNAP", message, path, details)


class StaleVersionError(JoinLabError):
    def __init__(self, message: str, path: str = "", **details: Any) -> None:
        super().__init__("E_STALE", message, path, details)
