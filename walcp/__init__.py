"""Deterministic WAL replay, checkpoint interoperability, and recovery toolkit."""

from .errors import AppError, ErrorCode

__all__ = ["AppError", "ErrorCode"]
__version__ = "1.0.0"
