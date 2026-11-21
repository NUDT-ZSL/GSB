"""Deterministic SQLite lock-wait and index-attribution diagnostics."""

from .errors import DiagnosticError
from .validation import load_and_validate

__all__ = ["DiagnosticError", "load_and_validate"]

__version__ = "1.0.0"
