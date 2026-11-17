"""Business errors used by every public component boundary."""

from __future__ import annotations

from typing import Any, Mapping


class DomainError(Exception):
    """A deterministic, machine-readable business error.

    ``file`` is always a stable display identifier (normally the basename of
    the offending input), while ``line`` is one-based for text inputs.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        file: str | None = None,
        line: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.file = file
        self.line = line
        self.detail = dict(detail or {})
        super().__init__(self._render())

    def _render(self) -> str:
        location = ""
        if self.file is not None:
            location += f" file={self.file}"
        if self.line is not None:
            location += f" line={self.line}"
        suffix = f" ({location.strip()})" if location else ""
        return f"[{self.code}] {self.message}{suffix}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "detail": self.detail,
        }


def spec_error(
    message: str,
    *,
    file: str | None = None,
    line: int | None = None,
    detail: Mapping[str, Any] | None = None,
) -> DomainError:
    return DomainError("E_SPEC", message, file=file, line=line, detail=detail)
