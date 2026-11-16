"""Business error codes and exceptions for the drift convergence workbench.

Error codes
-----------
E_DECL      : a declaration group file is invalid and rejected as a whole.
E_REF       : an operation violates an existing reference (e.g. delete in use).
E_CYCLE     : the reference dependency graph contains a cycle.
E_REPLAY    : a snapshot cannot be deterministically replayed.
E_CONVERGE  : a convergence action fails / is skipped; recorded in the trace.

All raised exceptions carry a stable ``code`` and a structured ``details``
payload so the GUI and the demo report can render them deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class WorkbenchError(Exception):
    """Base class for all business errors raised by the workbench."""

    code = "E_UNKNOWN"

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class DeclarationError(WorkbenchError):
    """A declaration group is invalid; the whole group load is rejected."""

    code = "E_DECL"

    def __init__(
        self,
        message: str,
        *,
        file: str = "",
        field_path: str = "",
        details: dict[str, Any] | None = None,
    ):
        payload: dict[str, Any] = dict(details or {})
        if file:
            payload["file"] = file
        if field_path:
            payload["field_path"] = field_path
        location = ""
        if file:
            location = f"{file}{('::' + field_path) if field_path else ''}"
        full = f"{self.code}: {message}"
        if location:
            full = f"{self.code} [{location}]: {message}"
        super().__init__(full, payload)
        self.file = file
        self.field_path = field_path


class ReferenceError(WorkbenchError):
    """An operation breaks an existing resource reference."""

    code = "E_REF"


class CycleError(WorkbenchError):
    """The resource reference graph contains a directed cycle."""

    code = "E_CYCLE"


class ReplayError(WorkbenchError):
    """A persisted snapshot cannot be replayed consistently."""

    code = "E_REPLAY"


@dataclass
class ConvergenceFailure:
    """Trace record emitted with the E_CONVERGE business code."""

    resource_key: str
    reason: str
    triggered_by: str = ""
    policy: str = ""
    code: str = "E_CONVERGE"
    expected: Any = None
    current: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "code": self.code,
            "resource_key": self.resource_key,
            "reason": self.reason,
            "triggered_by": self.triggered_by,
            "policy": self.policy,
            "expected": self.expected,
            "current": self.current,
        }
        data.update(self.extra)
        return data

