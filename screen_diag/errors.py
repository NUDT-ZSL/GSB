from __future__ import annotations

from typing import Any, Dict, Optional


EXIT_OK = 0
EXIT_SCHEMA = 2
EXIT_DIGEST = 3
EXIT_REPLAY = 4
EXIT_INTERRUPTED = 130

EXIT_CODES = {
    "OK": EXIT_OK,
    "E_SCHEMA": EXIT_SCHEMA,
    "E_DIGEST": EXIT_DIGEST,
    "E_REPLAY": EXIT_REPLAY,
    "E_INTERRUPTED": EXIT_INTERRUPTED,
}


class DiagnosticError(Exception):
    code = "E_INTERNAL"
    exit_code = 1

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_report(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class SchemaError(DiagnosticError):
    code = "E_SCHEMA"
    exit_code = EXIT_SCHEMA


class DigestValidationError(DiagnosticError):
    code = "E_DIGEST"
    exit_code = EXIT_DIGEST


class ReplayMismatchError(DiagnosticError):
    code = "E_REPLAY"
    exit_code = EXIT_REPLAY


class InterruptedAtBoundary(Exception):
    """Raised internally after a chunk boundary has been persisted."""

    exit_code = EXIT_INTERRUPTED
