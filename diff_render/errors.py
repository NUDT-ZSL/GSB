"""Stable error codes, process exit codes and JSON error envelopes.

Every error that reaches the command line is rendered as a single JSON object
of the form ``{"ok": false, "error": {"code": ..., "message": ..., ...}}``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional


class PipelineError(Exception):
    """Base class for all pipeline failures carrying a stable error code."""

    code = "E_PIPELINE"
    exit_code = 1

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def envelope(self) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"ok": False, "error": error}


class SchemaError(PipelineError):
    code = "E_SCHEMA"
    exit_code = 2


class ConvergenceError(PipelineError):
    code = "E_CONVERGE"
    exit_code = 3


class DigestError(PipelineError):
    code = "E_DIGEST"
    exit_code = 4


class WriteError(PipelineError):
    code = "E_WRITE"
    exit_code = 5


class TileError(PipelineError):
    code = "E_TILE"
    exit_code = 6


class InterruptedError(PipelineError):
    """Raised internally when a handled termination signal is requested."""

    code = "E_INTERRUPTED"
    exit_code = 143


def render_error(error: PipelineError) -> str:
    """Serialize an error envelope to a stable, newline-terminated JSON string."""
    return json.dumps(error.envelope(), ensure_ascii=False, sort_keys=True) + "\n"
