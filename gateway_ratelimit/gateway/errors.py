"""Stable business codes used by every gateway component."""

from __future__ import annotations

from dataclasses import dataclass

E_OK = "E_OK"
E_SPEC = "E_SPEC"
E_AUTH = "E_AUTH"
E_LIMIT = "E_LIMIT"
E_VERSION = "E_VERSION"
E_REPLAY = "E_REPLAY"
E_UNKNOWN = "E_UNKNOWN"


@dataclass
class BusinessError(Exception):
    """Error carrying a stable business code and an optional file/line locator."""

    code: str
    message: str
    file: str | None = None
    line: int | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        loc = f" {self.file}:{self.line}" if self.file else ""
        return f"[{self.code}]{loc} {self.message}"


def spec_error(message: str, file: str | None = None, line: int | None = None) -> BusinessError:
    return BusinessError(E_SPEC, message, file, line)
