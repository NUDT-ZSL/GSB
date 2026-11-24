"""Typed exceptions used at process and task boundaries."""

from __future__ import annotations


class DetectorError(Exception):
    code = "E_INTERNAL"

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


class InputError(DetectorError):
    code = "E_INPUT"


class ParameterError(DetectorError):
    code = "E_PARAM"


class LockUnavailable(DetectorError):
    code = "E_LOCK"


class DeadlineExceeded(DetectorError):
    code = "E_TIMEOUT"

