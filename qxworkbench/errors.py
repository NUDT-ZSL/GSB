"""Typed workbench errors with stable machine-readable codes."""

from __future__ import annotations

from typing import Any, Mapping


class WorkbenchError(Exception):
    """An expected, diagnosable failure.

    The CLI and tests rely on the ``code`` and ``details`` attributes rather
    than on exception text, so callers can make deterministic decisions.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


def unsupported_version(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError("E_VERSION_UNSUPPORTED", message, details=details)


def format_corrupt(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError("E_FORMAT_CORRUPT", message, details=details)


def resource_limit(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError("E_RESOURCE_LIMIT", message, details=details)


def version_skew(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError("E_VERSION_SKEW", message, details=details)


def inconsistent(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError("E_INCONSISTENT", message, details=details)
