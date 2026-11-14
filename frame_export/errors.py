"""Typed, readable errors used across the exporter."""

from __future__ import annotations

from typing import Any


class ExportError(Exception):
    """A failure that can be explained to the user and mirrored in the report."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class FrameFailure(Exception):
    """A single frame failed; the rest of the batch must continue."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
