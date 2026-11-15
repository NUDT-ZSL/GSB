from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class Event:
    event_time_us: int
    device_id: int
    sequence: int
    observation: float
    weight: float = 1.0

    @property
    def unique_key(self) -> tuple[int, int, int]:
        return (self.device_id, self.event_time_us, self.sequence)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Event":
        return cls(
            event_time_us=value["event_time_us"],
            device_id=value["device_id"],
            sequence=value["sequence"],
            observation=value["observation"],
            weight=value.get("weight", 1.0),
        )


@dataclass(frozen=True, slots=True)
class WindowStep:
    window_id: int
    state: str
    revision: int
    member_count: int
    sum_value: float
    sum_error_bound: float
    rms_value: float
    rms_error_bound: float
    weighted_mean: float
    weighted_mean_error_bound: float


@dataclass(frozen=True, slots=True)
class IngestResult:
    code: str = "OK"
    accepted: bool = False
    late: bool = False
    duplicate: bool = False
    event_key: tuple[int, int, int] | None = None
    status: str = "PENDING"
    window_ids: tuple[int, ...] = ()
    closed_window_ids: tuple[int, ...] = ()
    dropped_windows: tuple[tuple[int, int], ...] = ()
    steps: tuple[WindowStep, ...] = ()
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.code == "OK"

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["steps"] = [asdict(step) for step in self.steps]
        return result


@dataclass(frozen=True, slots=True)
class FlushResult:
    watermark_us: int
    closed_window_ids: tuple[int, ...] = ()

    @property
    def code(self) -> str:
        return "OK"


@dataclass(frozen=True, slots=True)
class WindowResult:
    window_id: int
    strategy: str
    start_us: int
    end_us: int
    state: str
    revision: int
    member_count: int
    sum_value: float
    sum_error_bound: float
    rms_value: float
    rms_error_bound: float
    weighted_mean: float
    weighted_mean_error_bound: float
    closed_watermark_us: int | None
    sum_total: float = 0.0
    sum_compensation: float = 0.0
    sum_absolute: float = 0.0
    weighted_sum_total: float = 0.0
    weighted_sum_compensation: float = 0.0
    weighted_sum_absolute: float = 0.0
    weight_total: float = 0.0
    weight_compensation: float = 0.0
    weight_absolute: float = 0.0
    square_sum_total: float = 0.0
    square_sum_compensation: float = 0.0
    square_sum_absolute: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ErrorReport:
    window: WindowResult
    reference_method: str
    truth_sum: float
    truth_rms: float
    truth_weighted_mean: float
    sum_deviation: float
    rms_deviation: float
    weighted_mean_deviation: float
    code: str
    bound_failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.code == "OK"

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["window"] = self.window.as_dict()
        return result

