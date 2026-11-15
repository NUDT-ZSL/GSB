from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from .errors import AggregationError, ErrorCode


@dataclass(frozen=True, slots=True)
class AggregatorConfig:
    """Window and fault-injection configuration.

    ``strict_event_time_order`` is opt-in because ordinary late events have an
    event time below the maximum already observed. When enabled, any backward
    step is rejected before it can alter persisted state.
    """

    strategy: Literal["fixed", "sliding"] = "fixed"
    window_length_us: int = 100_000
    allowed_lateness_us: int = 20_000
    slide_us: int | None = None
    strict_event_time_order: bool = False
    inject_db_conflict_once: bool = False
    busy_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if self.strategy not in ("fixed", "sliding"):
            raise AggregationError(
                ErrorCode.E_WINDOW_PARAM,
                f"unsupported strategy: {self.strategy!r}",
            )
        if not isinstance(self.window_length_us, int) or isinstance(
            self.window_length_us, bool
        ):
            raise AggregationError(
                ErrorCode.E_WINDOW_PARAM, "window_length_us must be an integer"
            )
        if self.window_length_us <= 0:
            raise AggregationError(
                ErrorCode.E_WINDOW_PARAM, "window_length_us must be positive"
            )
        if not isinstance(self.allowed_lateness_us, int) or isinstance(
            self.allowed_lateness_us, bool
        ):
            raise AggregationError(
                ErrorCode.E_WINDOW_PARAM, "allowed_lateness_us must be an integer"
            )
        if self.allowed_lateness_us < 0:
            raise AggregationError(
                ErrorCode.E_WINDOW_PARAM, "allowed_lateness_us must be non-negative"
            )
        if self.strategy == "sliding":
            if self.slide_us is None:
                raise AggregationError(
                    ErrorCode.E_WINDOW_PARAM,
                    "slide_us is required for sliding windows",
                )
            if not isinstance(self.slide_us, int) or isinstance(self.slide_us, bool):
                raise AggregationError(
                    ErrorCode.E_WINDOW_PARAM, "slide_us must be an integer"
                )
            if self.slide_us <= 0 or self.slide_us > self.window_length_us:
                raise AggregationError(
                    ErrorCode.E_WINDOW_PARAM,
                    "slide_us must satisfy 0 < slide_us <= window_length_us",
                )
        elif self.slide_us is not None:
            raise AggregationError(
                ErrorCode.E_WINDOW_PARAM,
                "slide_us is only valid for sliding windows",
            )
        if not isinstance(self.busy_timeout_ms, int) or self.busy_timeout_ms < 0:
            raise AggregationError(
                ErrorCode.E_WINDOW_PARAM, "busy_timeout_ms must be a non-negative integer"
            )

    @property
    def persistent_parameters(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "window_length_us": self.window_length_us,
            "allowed_lateness_us": self.allowed_lateness_us,
            "slide_us": self.slide_us,
        }

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

