from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class EngineError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int = 2,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }


@dataclass(frozen=True)
class Issue:
    code: str
    source: str
    message: str
    path: str = "$"
    index: int | None = None
    kind: str | None = None
    namespace: str | None = None
    name: str | None = None
    quarantined: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def resource_key(self) -> str | None:
        if self.kind and self.namespace and self.name:
            return f"{self.kind}/{self.namespace}/{self.name}"
        return None

    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.source,
            self.index if self.index is not None else -1,
            self.code,
            self.path,
            self.kind or "",
            self.namespace or "",
            self.name or "",
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "code": self.code,
            "source": self.source,
            "path": self.path,
            "message": self.message,
            "quarantined": self.quarantined,
            "resource": self.resource_key,
        }
        if self.index is not None:
            result["index"] = self.index
        if self.extra:
            result["details"] = self.extra
        return result

