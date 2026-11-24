from __future__ import annotations

from typing import Any


class WorkbenchError(Exception):
    """An error carrying a stable business code and serializable details."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        if not code.startswith("E_"):
            raise ValueError(f"business code must start with E_: {code!r}")
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
