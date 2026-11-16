"""Business error codes and structured exception type."""
from __future__ import annotations

from typing import Any, Mapping, Optional


class ErrorCode:
    E_DATA = "E_DATA"
    E_EXPR = "E_EXPR"
    E_TYPE = "E_TYPE"
    E_NULLMAP = "E_NULLMAP"
    E_FUSE = "E_FUSE"
    E_REPLAY = "E_REPLAY"
    E_BATCH = "E_BATCH"

    ALL = (
        E_DATA,
        E_EXPR,
        E_TYPE,
        E_NULLMAP,
        E_FUSE,
        E_REPLAY,
        E_BATCH,
    )


class BenchError(Exception):
    """An error carrying a stable business code and an audit-ready detail map."""

    def __init__(self, code: str, message: str, detail: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail: dict[str, Any] = dict(detail) if detail else {}

    def to_event(self) -> dict[str, Any]:
        event = {"code": self.code, "message": self.message}
        event.update(self.detail)
        return event

    def __str__(self) -> str:  # pragma: no cover - trivial
        rendered_detail = ", ".join(f"{key}={value}" for key, value in sorted(self.detail.items()))
        if rendered_detail:
            return f"[{self.code}] {self.message} ({rendered_detail})"
        return f"[{self.code}] {self.message}"
