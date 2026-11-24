"""Exception types carrying stable business codes."""

from __future__ import annotations

from typing import Any


class OslError(Exception):
    def __init__(self, code: str, message: str, offset: int | None = None, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.offset = offset
        self.details = details


class HeaderError(OslError):
    pass


class BoundsError(OslError):
    pass


class BudgetTimeout(OslError):
    pass


class QuotaExceeded(OslError):
    pass


class StorageError(OslError):
    pass


class LockConflict(StorageError):
    pass
