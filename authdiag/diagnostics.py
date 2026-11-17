"""Business diagnostics with source offsets and stable ordinals."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .constants import NON_FATAL_CODES


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    source: str = ""
    index: int | None = None
    offset: int | None = None
    stable_key: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    severity: str = "error"

    @property
    def ordinal(self) -> int | None:
        return None if self.index is None else self.index + 1

    @property
    def is_error(self) -> bool:
        if self.severity == "warning":
            return False
        return self.code not in NON_FATAL_CODES

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "index": self.index,
            "ordinal": self.ordinal,
            "offset": self.offset,
            "stable_key": self.stable_key,
            "severity": self.severity,
            "details": _stable_value(self.details),
        }


def _stable_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _stable_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_stable_value(item) for item in value]
    if isinstance(value, set):
        return sorted(_stable_value(item) for item in value)
    return value


def sort_diagnostics(items: Iterable[Diagnostic]) -> list[Diagnostic]:
    return sorted(
        items,
        key=lambda item: (
            item.source,
            -1 if item.offset is None else item.offset,
            -1 if item.index is None else item.index,
            item.code,
            item.stable_key,
            item.message,
        ),
    )


def has_errors(items: Iterable[Diagnostic]) -> bool:
    return any(item.is_error for item in items)
