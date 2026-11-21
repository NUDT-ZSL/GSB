from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


def require_int(value: object, field_name: str, *, code: str = "E_BAD_SNAPSHOT") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        from .errors import CertLabError

        raise CertLabError(code, f"{field_name} must be an integer", {"field": field_name})
    return value


@dataclass(frozen=True)
class LogicalClock:
    """An injected millisecond clock. It never consults the system wall clock."""

    now_ms: int

    def __post_init__(self) -> None:
        require_int(self.now_ms, "clock_ms", code="E_INVALID_ARGUMENT")

    def datetime_utc(self) -> datetime:
        return datetime.fromtimestamp(self.now_ms / 1000, tz=timezone.utc)


def datetime_to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp() * 1000)

