from __future__ import annotations


class WorkbenchError(Exception):
    """An error carrying a stable business code."""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ConfigInvalid(WorkbenchError):
    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__("E_CONFIG_INVALID", message, details=details)


class SnapshotMismatch(WorkbenchError):
    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__("E_CONFIG_MISMATCH", message, details=details)
