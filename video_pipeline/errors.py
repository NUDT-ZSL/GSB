"""Pipeline-specific exceptions."""

from __future__ import annotations


class PipelineError(Exception):
    """Base class for handled pipeline failures."""


class ConfigError(PipelineError):
    """Raised when a required configuration field is missing or invalid."""

    def __init__(self, errors: list[str] | tuple[str, ...]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


class DecodeError(PipelineError):
    """Raised when a video frame cannot be decoded."""

    def __init__(self, frame_index: int, message: str):
        self.frame_index = frame_index
        super().__init__(message)


class SnapshotConfigMismatch(PipelineError):
    """Raised when a snapshot belongs to another semantic configuration."""


class SnapshotFormatError(PipelineError):
    """Raised when a snapshot cannot be parsed or has an unsupported shape."""
