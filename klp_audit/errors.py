"""Business error codes and typed exceptions."""

from __future__ import annotations

E_FORMAT = "E_FORMAT"
E_CRC = "E_CRC"
E_STAGE = "E_STAGE"
E_ALGORITHM = "E_ALGORITHM"
E_PARAM = "E_PARAM"
E_TRANSITION = "E_TRANSITION"
E_INCOMPLETE = "E_INCOMPLETE"
E_SEQ_CONFLICT = "E_SEQ_CONFLICT"
E_SALT_REUSE = "E_SALT_REUSE"
E_LENGTH_ESCALATION = "E_LENGTH_ESCALATION"
E_ITER_REGRESSION = "E_ITER_REGRESSION"
E_TIMEOUT = "E_TIMEOUT"
E_BUDGET = "E_BUDGET"
E_LOCK = "E_LOCK"
E_IO = "E_IO"
E_INPUT = "E_INPUT"


class KlpError(Exception):
    """Base exception carrying a stable business code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class KlpConfigError(KlpError):
    """Invalid command-line or engine configuration."""


class KlpFormatError(KlpError):
    """A file-level protocol framing error."""


class KlpLockError(KlpError):
    """A workspace file lock could not be acquired."""


class KlpInputError(KlpError):
    """An input path or byte payload cannot be used."""

