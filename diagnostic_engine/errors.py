"""Typed error codes used by the engine and its CLI."""

from __future__ import annotations

from typing import Any


class DiagnosticError(Exception):
    """An error with one of the stable ``E_*`` codes from the specification."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.code,
            "error": self.message,
            "details": self.details,
        }


def bad_input(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError("E_BAD_INPUT", message, details)


def cross_def(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError("E_CROSS_DEF", message, details)


def mem_limit(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError("E_MEM_LIMIT", message, details)


def std_zero(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError("E_STD_ZERO", message, details)


def intermediate_dim(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError("E_INTERMEDIATE_DIM", message, details)


def drift_missing(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError("E_DRIFT_MISSING", message, details)


def quantile_order(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError("E_QUANTILE_ORDER", message, details)


def precision_invalid(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError("E_PRECISION_INVALID", message, details)


def stage_busy(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError("E_STAGE_BUSY", message, details)


def stale_result(message: str, **details: Any) -> DiagnosticError:
    return DiagnosticError("E_STALE_RESULT", message, details)
